#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键出包：后端 exe + NapCat 离线包 + Tauri 安装包 + 绿色版 zip。

用法（在项目根目录）：
    python 打包发布.py                # 完整流程
    python 打包发布.py --skip-napcat  # 不检查/不下载离线包（体积会小很多）
    python 打包发布.py --no-install   # 只打后端，不出安装包（调试用）

它做的事，按顺序：
    1. build_napcat.py   把 NapCat 离线包放进 desktop/src-tauri/resources/
    2. build_backend.py  PyInstaller 打 backend_server.exe（含 edge-tts）
    3. npm run tauri build  出 NSIS 安装包
    4. 复制安装包到 release\\小鲸鱼_x64-setup.exe
    5. 用 Python 的 zipfile 打绿色版（**不要用 tar.exe** —— 它打的流式 zip
       资源管理器会报「压缩文件夹无效」，V1.2.1 踩过这个坑）

打完把 release\\ 里两个文件发出去就行。
"""

import argparse
import hashlib
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.join(ROOT, "desktop")
SRC_TAURI = os.path.join(DESKTOP, "src-tauri")
RELEASE_DIR = os.path.join(ROOT, "release")
DIST_DIR = os.path.join(ROOT, "dist")
NSIS_DIR = os.path.join(SRC_TAURI, "target", "release", "bundle", "nsis")
RES_DIR = os.path.join(SRC_TAURI, "resources")

# v1.9.7.1：恢复工具的几个文件（打包时顺手核对验签器哈希）
TOOLS_DIR = os.path.join(ROOT, "Tools")
TOOLS_ZIP_FILES = ("恢复官方安装.ps1", "恢复官方安装.bat", "verify_bundle.exe", "使用说明.txt")


def log(msg):
    print(msg, flush=True)


def run(cmd, cwd=None, env=None):
    log("$ " + (" ".join(cmd) if isinstance(cmd, list) else str(cmd)))
    p = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str))
    if p.returncode != 0:
        raise SystemExit("命令失败（退出码 %s）：%s" % (p.returncode, cmd))


def npm_cmd():
    """Windows 上 npm 是 npm.cmd；找不到就用 PATH 里的。"""
    for cand in ("npm.cmd", "npm"):
        found = shutil.which(cand)
        if found:
            return found
    p = r"C:\Program Files\nodejs\npm.cmd"
    if os.path.isfile(p):
        return p
    raise SystemExit("找不到 npm，请先装 Node.js（或把 C:\\Program Files\\nodejs 加进 PATH）")


def build_env():
    env = dict(os.environ)
    cargo = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
    node = r"C:\Program Files\nodejs"
    parts = [p for p in (node, cargo) if os.path.isdir(p)]
    env["PATH"] = ";".join(parts + [env.get("PATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # v1.9.7：可复现链接。MSVC 默认把当前时间写进 PE 头，导致每次重新链接出来的
    # exe 字节都不同 —— 而「清单里签主程序哈希」要求构建前后一致，所以必须让链接
    # 只由输入决定。`/Brepro` 是 MSVC 的 reproducible build 开关。
    # 若环境变量里已经有 RUSTFLAGS，就追加而不是覆盖。
    _flags = (env.get("RUSTFLAGS") or "").strip()
    if "/Brepro" not in _flags:
        env["RUSTFLAGS"] = (_flags + " -C link-arg=/Brepro").strip()
    return env


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    return "%.2f MB" % (n / 1024.0 / 1024.0)


def step_bundle(skip=False):
    if skip:
        log("[1/5] 跳过 NapCat 离线包检查（--skip-napcat）")
        return
    run([sys.executable, os.path.join(ROOT, "build_napcat.py")], cwd=ROOT, env=build_env())


def step_backend():
    run([sys.executable, os.path.join(ROOT, "build_backend.py"), "--clean"],
        cwd=ROOT, env=build_env())


def sync_av_module():
    """把项目根的 resources/av_module 同步到 src-tauri/resources/。

    v1.3.2（第四阶段）：tauri.conf.json 里的 resources 是相对 `src-tauri/` 解析的，
    所以必须在 build 之前把目录拷过去，否则报
    `resource path resources\\av_module doesn't exist`。
    """
    import shutil as _sh
    src = os.path.join(ROOT, "resources", "av_module")
    dst = os.path.join(RES_DIR, "av_module")
    if not os.path.isdir(src):
        log("    注意：项目根没有 resources/av_module，安装包里不会带音视频模块")
        return False
    if os.path.isdir(dst):
        _sh.rmtree(dst, ignore_errors=True)
    # 注意：runtime/ 里有 whisper 模型（500MB+）和 TTS 缓存，绝不能进安装包；
    # 打包版会把它放到 %LOCALAPPDATA%\小鲸鱼\av_module\runtime，首次用到再下载。
    # v1.9.0：tools/ 是探测设备、试音、造样例这类调试脚本，tests/ 是自测脚本 ——
    # 都不进安装包（既占地方，也把内部实现和测试用地址一起交出去）。
    _sh.copytree(src, dst, ignore=_sh.ignore_patterns(
        "__pycache__", "*.pyc", "runtime", "build", "dist", "*.spec",
        "*.bak_*", "*.bak", ".git", "_snapshot*.png",
        "config.json", "voices", "tools", "tests",
        "_t*.py", "_p*.py", "_test_*.py"))
    n = sum(len(f) for _r, _d, f in os.walk(dst))
    size = sum(os.path.getsize(os.path.join(_r, f)) for _r, _d, fs in os.walk(dst) for f in fs)
    log("    音视频模块已同步到 src-tauri/resources（%d 个文件，%.1f MB）"
        % (n, size / 1048576.0))
    exe = os.path.join(dst, "av_server.exe")
    if os.path.isfile(exe):
        log("    其中 av_server.exe = %.1f MB（用户无需安装 Python）"
            % (os.path.getsize(exe) / 1048576.0))
    else:
        log("    警告：没有 av_server.exe，用户机器上没装 Python 时音视频会降级")
    return True


def step_tauri():
    """v1.9.7：两轮构建。

    第一轮编译出 `小鲸鱼.exe`（此时清单里还没有它），第二轮把它的哈希写进清单
    再打包。第二轮因为 cargo 已 fresh，通常只是重新走一遍 bundler，不会改变
    主程序字节。打完核对「清单里的哈希 == 实际产物哈希」，不一致就重签再打，
    三次还不行就中止 —— 宁可不出包，也不能出一个自己会判自己篡改的包。
    """
    mp = os.path.join(RES_DIR, "manifest.sha256")
    # V1.9.8-Beta：先把上一轮留下的安装包删掉。Tauri 的 NSIS 步骤是「覆盖写」，
    # 旧文件被杀软实时扫描按住时会报 os error 32（另一程序正在使用此文件），
    # 而且这个错误发生在 makensis 真正开始之前，会让整次打包白跑一遍。
    if os.path.isdir(NSIS_DIR):
        for f in list(os.listdir(NSIS_DIR)):
            if not f.lower().endswith(".exe"):
                continue
            p = os.path.join(NSIS_DIR, f)
            for _ in range(5):
                try:
                    os.remove(p)
                    log("    已清掉上一轮的安装包：%s" % f)
                    break
                except Exception:
                    time.sleep(2.0)
            else:
                log("    ！清不掉上一轮的安装包（%s），如果后面报 os error 32 就是它" % f)
    for attempt in range(1, 4):
        sign_tauri_resources()
        want = manifest_exe_hash(mp)
        log("    [第 %d 轮] 清单里的主程序哈希：%s"
            % (attempt, (want[:16] + "…") if want else "（还没有，等这次编出来）"))
        started = time.time()
        try:
            run([npm_cmd(), "run", "tauri", "build"], cwd=DESKTOP, env=build_env())
        except (Exception, SystemExit) as e:
            # V1.9.8-Beta：Tauri 打完 NSIS 之后偶尔会因为「安装包正被杀软扫描」
            # 报 os error 32（另一个程序正在使用此文件），但其实安装包已经生成了。
            # 判定标准：NSIS 目录里有本次构建之后新写出来的 setup.exe 就算成功。
            fresh = ""
            if os.path.isdir(NSIS_DIR):
                for f in os.listdir(NSIS_DIR):
                    p = os.path.join(NSIS_DIR, f)
                    if f.lower().endswith(".exe") and os.path.getmtime(p) >= started - 2:
                        if not fresh or os.path.getmtime(p) > os.path.getmtime(fresh):
                            fresh = p
            if not fresh:
                raise
            log("    ！Tauri 报了打包错误（%s），但安装包已经生成：%s"
                % (str(e).split("\n")[0][:120], os.path.basename(fresh)))
        # V1.9.7.1（P0-补）：比对的是「装完之后」的哈希 —— 安装包里的主程序
        # 会被 bundler 改 3 个字节，拿构建产物的原始哈希去比永远不相等。
        have = sha256_of_installed_exe(built_exe_path())
        if want and have and want == have:
            log("    主程序与清单一致（%s…，已按安装包内的字节计算），打包完成" % have[:16])
            return
        if not have:
            raise RuntimeError("找不到构建产物 %s" % built_exe_path())
        log("    构建后主程序哈希变了（清单 %s / 实际 %s），重签再打一次"
            % (want[:16] or "空", have[:16]))
    raise RuntimeError("三次尝试后清单里的主程序哈希仍与构建产物不一致，停止打包")


def step_collect():
    os.makedirs(RELEASE_DIR, exist_ok=True)
    found = []
    if os.path.isdir(NSIS_DIR):
        for f in os.listdir(NSIS_DIR):
            if f.lower().endswith(".exe"):
                found.append(os.path.join(NSIS_DIR, f))
    if not found:
        raise SystemExit("没找到安装包，检查 %s" % NSIS_DIR)
    src = max(found, key=os.path.getmtime)
    dst = os.path.join(RELEASE_DIR, "小鲸鱼_x64-setup.exe")
    shutil.copy2(src, dst)
    log("[4/5] 安装包 -> release\\小鲸鱼_x64-setup.exe（%s）" % human(os.path.getsize(dst)))
    return dst


def step_green_zip():
    """绿色版：Tauri 壳 exe + resources + 说明文件，zipfile 打包（资源管理器可解）。"""
    exe = os.path.join(SRC_TAURI, "target", "release", "小鲸鱼.exe")
    if not os.path.isfile(exe):
        exe = os.path.join(SRC_TAURI, "target", "release", "desktop.exe")
    if not os.path.isfile(exe):
        raise SystemExit("没找到桌面端 exe，先跑 npm run tauri build")
    backend = os.path.join(RES_DIR, "backend_server.exe")
    if not os.path.isfile(backend):
        raise SystemExit("没找到 %s，先打后端" % backend)
    bundle = os.path.join(RES_DIR, "napcat_bundle.zip")

    version = "unknown"
    try:
        import re
        # V1.3.2（BUG-02）：版本号只在 version.py 维护，退一步再看 backend_server.py
        for _f in ("version.py", "backend_server.py"):
            with open(os.path.join(ROOT, _f), "r", encoding="utf-8") as f:
                # 必须锚在行首：version.py 的文档字符串里也写着
                # 「config.APP_VERSION = "1.1.6"」，不加 ^ 会先匹配到那句注释（踩过）
                m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                version = m.group(1)
                break
    except Exception:
        pass

    zip_path = os.path.join(RELEASE_DIR, "小鲸鱼_V%s_绿色版.zip" % version)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    os.makedirs(RELEASE_DIR, exist_ok=True)

    entries = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(exe, "小鲸鱼.exe")
        entries += 1
        z.write(backend, os.path.join("resources", "backend_server.exe"))
        entries += 1
        if os.path.isfile(bundle):
            z.write(bundle, os.path.join("resources", "napcat_bundle.zip"))
            entries += 1
            log("    已带上 NapCat 离线包（%s）" % human(os.path.getsize(bundle)))
        else:
            log("    注意：没有 NapCat 离线包，绿色版里将退化成联网下载")
        # v1.3.2（第四阶段）：音视频模块随绿包一起放（后端 exe 里也有一份，
        # 但放在这儿用户能直接看到、也方便自己改/换版本）
        av_dir = os.path.join(RES_DIR, "av_module")
        if os.path.isdir(av_dir):
            _n = 0
            for _dp, _dn, _fn in os.walk(av_dir):
                # v1.9.0：tools/tests 是调试与自测脚本，兜一层 —— 就算
                # src-tauri/resources 里的副本是旧版同步留下的，也不会被打进去。
                _dn[:] = [d for d in _dn
                          if d not in ("__pycache__", "tools", "tests")]
                for _f in _fn:
                    _src = os.path.join(_dp, _f)
                    _rel = os.path.relpath(_src, RES_DIR).replace("\\", "/")
                    z.write(_src, os.path.join("resources", _rel))
                    entries += 1
                    _n += 1
            log("    已带上音视频模块（%d 个文件）" % _n)
        for name in ("README.md", "使用说明.txt", "使用说明_EN.txt"):
            p = os.path.join(ROOT, name)
            if os.path.isfile(p):
                z.write(p, name)
                entries += 1

    # 自检：条目数必须 > 0（tar.exe 那个流式 zip 的教训）
    with zipfile.ZipFile(zip_path) as z:
        got = len(z.namelist())
        bad = z.testzip()
    if got != entries or bad:
        raise SystemExit("绿色版 zip 自检失败：条目 %s（应为 %s），坏条目 %s" % (got, entries, bad))
    sign_zip(zip_path)
    log("[5/5] 绿色版 -> %s（%s，%s 个条目）"
        % (os.path.basename(zip_path), human(os.path.getsize(zip_path)), got))
    return zip_path


def built_exe_path():
    """Tauri 编出来的主程序（打包前它就已经在 target/release 里）。"""
    return os.path.join(SRC_TAURI, "target", "release", "小鲸鱼.exe")


def sha256_of_file(p):
    """分块算 sha256，返回小写 hex；读不到返回空串。"""
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return ""


_TAURI_BUNDLE_UNK = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
_TAURI_BUNDLE_NSS = b"__TAURI_BUNDLE_TYPE_VAR_NSS"


def sha256_of_installed_exe(p):
    """V1.9.7.1（P0-补）：算出「装完之后」那个主程序的 sha256。

    Tauri 的 NSIS bundler 在把 exe 塞进安装包时，会把 exe 里的包类型标记
    `__TAURI_BUNDLE_TYPE_VAR_UNK` 就地改成 `__TAURI_BUNDLE_TYPE_VAR_NSS`
    （两个字符串一样长，只改 3 个字节；`tauri_utils::platform::bundle_type()`
    靠它认出自己是 NSIS 装的）。target/release 里那份是 UNK，安装目录里那份
    是 NSS —— 大小完全一样，只差这 3 个字节。

    于是「清单里写 target/release 那份的哈希」的安装包，装完必然自判篡改：
    V1.9.7 的 P0 就是这么来的（装完首屏遮罩，报「小鲸鱼.exe 内容被改过」）。
    绿色版没这个问题（zip 里就是 UNK 那份，一个字节都没动）。

    所以清单里「小鲸鱼.exe」这一行必须写**装上之后的字节**：先把标记换成
    NSS 再算哈希。这里只在内存里换字节，绝不改动构建产物 —— 绿色版那份仍是
    UNK，与 target/release 完全一致。
    """
    try:
        with open(p, "rb") as f:
            data = f.read()
    except Exception:
        return ""
    if data.count(_TAURI_BUNDLE_UNK) == 1:
        data = data.replace(_TAURI_BUNDLE_UNK, _TAURI_BUNDLE_NSS)
    return hashlib.sha256(data).hexdigest()


def swap_manifest_hash(text, rel, new_hash):
    """把清单里某一行（相对路径 == rel）的哈希换掉，行序与格式保持不变。"""
    out, hit = [], False
    for line in text.split("\n"):
        parts = line.split("  ", 1)
        if len(parts) == 2 and parts[1].strip().lower() == rel.lower():
            out.append("%s  %s" % (new_hash, parts[1].rstrip("\r")))
            hit = True
        else:
            out.append(line)
    if not hit:
        log("    ！清单里没有 %s —— 这个包装上会自判篡改" % rel)
    return "\n".join(out)


# v1.9.9：清单只签「程序本体」。以前 sign_zip() 把绿色版 zip 里所有条目
# （README.md / 使用说明 / RPA 模板 PNG / resources 下的 .py …）全签进清单，
# 结果用户改个错别字、换个说明文件，启动就被判「文件被改过」—— 既拦不住
# 真正的篡改（改 exe 才是要害），又把老实人坑死。现在只认这几类：
#   小鲸鱼.exe、resources/backend_server.exe、resources/av_module/av_server.exe、
#   以及编译产物 *.pyd（它们是真代码）。
SIGN_CORE_FILES = (
    "小鲸鱼.exe",
    "resources/backend_server.exe",
    "resources/av_module/av_server.exe",
)


def is_signed_core(rel):
    """这个相对路径要不要进清单（v1.9.9：只签程序本体）。"""
    r = str(rel or "").replace("\\", "/").strip()
    while r.startswith("./"):
        r = r[2:]
    if not r:
        return False
    if r in SIGN_CORE_FILES:
        return True
    return r.lower().endswith(".pyd")


def install_manifest_text(green_text):
    """把绿色版清单筛成「安装形态里真实存在的文件」。

    NSIS 安装包只装主程序本身和 resources\\ 下的东西，不装 README.md /
    使用说明.txt / 使用说明_EN.txt（这三样只在绿色版 zip 里）。清单里留着这些行，
    装完再 `--verify-self` 就会报「README.md（文件不见了）」—— V1.9.8 首轮的
    安装版增量包正是这么踩的：清单是从 35 条的绿色版清单直接换哈希得来的。
    所以安装版清单必须先筛再签：只留「小鲸鱼.exe」与 resources/ 开头的条目。
    """
    keep = []
    for line in green_text.split("\n"):
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        _h, _rel = parts[0].strip(), parts[1].strip()
        _rl = _rel.replace("\\", "/").lower()
        if not _h or not _rel:
            continue
        if is_signed_core(_rel):
            keep.append("%s  %s" % (_h, _rel))
    return "\n".join(keep) + "\n" if keep else ""


def manifest_exe_hash(mp):
    """从清单里取「小鲸鱼.exe」那一行的哈希（没有就返回空串）。"""
    try:
        for line in open(mp, encoding="utf-8").read().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) == 2 and parts[1].strip().lower() == "小鲸鱼.exe":
                return parts[0].strip().lower()
    except Exception:
        pass
    return ""


def sign_tauri_resources():
    """给 desktop/src-tauri/resources 生成清单与签名，供壳在启动时校验。

    v1.9.7 起清单**必须包含主程序 小鲸鱼.exe**（V1.9.6 只列三个 resources 文件，
    结果那份公开的清单能被拿来给改过的主程序作保 —— 复测报告 C 组）。
    构建期主程序已经编译好了，把它临时摆到 root 下算哈希即可，不存在循环信任。
    返回 True 表示清单已就位（没私钥时仍写清单，只是不签名 → 壳会判「缺签名」）。
    """
    import base64
    try:
        import signing
    except Exception as e:
        log("签名模块不可用，跳过 resources 清单：%s" % e)
        return False
    root = SRC_TAURI
    res = RES_DIR
    if not os.path.isdir(res):
        log("没有 desktop/src-tauri/resources，跳过 resources 清单")
        return False
    pats = []
    # v1.9.9：napcat_bundle.zip 不再进清单（它是运行时资源，不是程序本体）
    for rel in ("resources/backend_server.exe",
            "resources/av_module/av_server.exe"):
        if os.path.isfile(os.path.join(root, rel)):
            pats.append(rel)
    # v1.9.7（P0）：清单必须覆盖**主程序**。
    # 以前这里写着「小鲸鱼.exe 不可能把自己算进自己的清单里」—— 这个说法不成立：
    # 清单是构建期生成的，那时主程序已经编译好了，不存在循环信任（循环只发生在
    # "运行时用被改过的自己去验自己"）。不覆盖主程序的后果是致命的：安装包的清单
    # 只列 3 个 resources 文件，而那份清单是公开的 —— 任何人把它放进程序目录，
    # 被改过的主程序就能完全冒充官方（复测报告 3.3 的 C 组）。
    stage = os.path.join(root, "小鲸鱼.exe")
    exe = built_exe_path()
    staged = False
    if os.path.isfile(exe):
        try:
            import shutil as _sh2
            _sh2.copy2(exe, stage)
            staged = True
            pats.append("小鲸鱼.exe")
        except Exception as e:
            log("    ！主程序临时摆放失败，清单里将不含主程序：%s" % e)
    if not pats:
        log("resources 里没有需要签名的文件")
        return False
    key_b64 = _sign_key_b64()
    # 清单写在 <root>/resources/ 里（Tauri 会把它装到 <安装目录>/resources/）
    text = signing.build_manifest(root, pats)
    # V1.9.7.1（P0-补）：这一份清单是**安装包**里的，主程序哈希要按「装完之后」
    # 的字节算（bundler 会改 3 个字节，见 sha256_of_installed_exe）。绿色版那份
    # 清单由 sign_zip() 另外生成，仍按构建产物算 —— 两个布局的清单本来就该不同。
    _inst = sha256_of_installed_exe(exe)
    if _inst:
        text = swap_manifest_hash(text, "小鲸鱼.exe", _inst)
    else:
        log("    ！算不出安装形态的主程序哈希，这份清单装完会自判篡改")
    mp = os.path.join(res, "manifest.sha256")
    with open(mp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    sp = os.path.join(res, "manifest.sig")
    if key_b64:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey)
        priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(key_b64))
        sig = priv.sign(text.encode("utf-8"))
        with open(sp, "wb") as f:
            f.write(sig)
        log("resources 清单已签名：%d 个文件" % len(text.strip().split("\n")))
    else:
        if os.path.isfile(sp):
            os.remove(sp)
        log("resources 清单已写（没有私钥，未签名）：%d 个文件"
            % len(text.strip().split("\n")))
    if staged:
        try:
            os.remove(stage)
        except Exception:
            pass
    return True


def sign_zip(zip_path):
    """v1.9.0：把 manifest.sha256 / manifest.sig 写进绿色版 zip。

    私钥规则同 sign_release：环境变量 XJY_SIGN_KEY 优先，其次项目外密钥文件；
    都没有就只写清单不签名（自己编译的包按「未签名」处理，不拦启动）。
    """
    import base64
    try:
        import signing
    except Exception as e:
        log("签名模块不可用，跳过：%s" % e)
        return False
    try:
        entries = {}
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("/") or n in ("manifest.sha256", "manifest.sig"):
                    continue
                # v1.9.9：文档/图片/配置不进清单（见 SIGN_CORE_FILES 的注释）
                if not is_signed_core(n):
                    continue
                h = hashlib.sha256()
                with z.open(n) as f:
                    while True:
                        b = f.read(1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                entries[n] = h.hexdigest()
        lines = ["%s  %s" % (v, k) for k, v in sorted(entries.items(),
                                                       key=lambda kv: kv[0])]
        text = "\n".join(lines) + "\n"
        key_b64 = _sign_key_b64()
        sig = b""
        if key_b64:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey)
            priv = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(key_b64)[:32])
            sig = priv.sign(text.encode("utf-8"))
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.sha256", text)
            if sig:
                z.writestr("manifest.sig", sig)
        log("    已写入清单：%d 个文件%s" % (len(entries),
                                        "，已签名" if sig else "（未签名：没有私钥）"))
        return True
    except Exception as e:
        log("写清单失败（不影响出包，但这个包只能按未签名处理）：%s" % e)
        return False


def app_version():
    """APP_VERSION 从 version.py 里读（BUG-02：全项目唯一来源）。"""
    try:
        import re
        for _f in ("version.py", "backend_server.py"):
            with open(os.path.join(ROOT, _f), "r", encoding="utf-8") as f:
                # 锚在行首：version.py 的文档串里也写着旧版本号，不锚会先匹配注释（踩过）
                m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "unknown"


def _drop_build_secret():
    """v1.9.1：兜底删掉构建期密钥文件（正常流程由 build_backend 收尾清理）。"""
    p = os.path.join(ROOT, "_build_secret.py")
    if os.path.isfile(p):
        try:
            os.remove(p)
            print("    已删除残留的 _build_secret.py（构建期密钥不进源码包）")
        except Exception as e:
            print("    ！删不掉 _build_secret.py：%s" % e)


def step_source_zip(version):
    _drop_build_secret()
    """开源包：源码 + 前端 + 文档 + 自检脚本，不含任何运行时数据/用户配置/编译产物。

    按 secure_core.py 里的约定：安全核心**不进源码包**，换成一份《安全核心说明.txt》。
    """
    zip_path = os.path.join(RELEASE_DIR, "小鲸鱼_V%s_开源包.zip" % version)
    os.makedirs(RELEASE_DIR, exist_ok=True)

    # 根目录：只挑源码/脚本/说明，绝不把 config.json、agent.log、memory.db 带出去
    root_files = []
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in (".py", ".bat", ".txt") or name in ("README.md", "index.html"):
            # v2.0.1（P0）：根目录的 `_打头` 文件全是临时脚本（自测探针、核对脚本、备份），
            # 它们依赖开发机的临时数据、也不属于产品源码 —— V2.0.0 的开源包里就混进了
            # `_v200a.py` / `_verify200.py` 这类东西，很难看。一律不带。
            if name.startswith("_"):
                continue
            root_files.append(name)

    # 明确不打包的东西
    SKIP_FILES = {"secure_core.py", "config.json", "agent_state.json", "learn.db",
                  "agent.log", "backend_server.spec", "build_version_info.txt", "manifest.sha256", "manifest.sig",
                  # v1.9.1：构建期随机密钥（BUILD_SECRET）。它只该活在产物里，
                  # 跟着开源包发出去等于把盐的派生密钥一起交出去。
                  "_build_secret.py"}
    root_files = [n for n in root_files if n not in SKIP_FILES]

    # 目录白名单（相对项目根的路径前缀）
    DIRS = [
        # v2.0.1（P0 修复）：V2.0.0 把 agent.py 拆成了 core/ 九个模块，但这份白名单没跟上，
        # 结果开源包里只有 agent.py、没有 core/ —— 拿源码包一跑就是
        # `ModuleNotFoundError: No module named 'core'`。以后新增包目录，记得同时加到这儿。
        "core",
        "Docs", "tests", "icons", "langs", "themes",
        os.path.join("desktop", "src"),
        os.path.join("desktop", "src-tauri", "src"),
        os.path.join("desktop", "src-tauri", "capabilities"),
        os.path.join("desktop", "src-tauri", "icons"),
        os.path.join("desktop", ".cargo"),
        os.path.join("resources", "av_module"),   # v1.3.2：音视频模块源码
        "Tools",   # v1.9.6：恢复工具（给用户的一键修复脚本）
    ]
    FILE_PATTERNS = [
        os.path.join("desktop", "package.json"),
        os.path.join("desktop", "README.md"),
        os.path.join("desktop", "index.html"),
        os.path.join("desktop", "src-tauri", "Cargo.toml"),
        os.path.join("desktop", "src-tauri", "Cargo.lock"),
        os.path.join("desktop", "src-tauri", "tauri.conf.json"),
        os.path.join("desktop", "src-tauri", "build.rs"),
    ]
    BAD_DIR = {"node_modules", "target", "dist", "build", "__pycache__", ".git",
               "resources", "release", "legacy", "build_napcat"}

    entries = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in root_files:
            z.write(os.path.join(ROOT, name), "小鲸鱼_V%s/%s" % (version, name))
            entries += 1
        for rel in FILE_PATTERNS:
            p = os.path.join(ROOT, rel)
            if os.path.isfile(p):
                z.write(p, "小鲸鱼_V%s/%s" % (version, rel.replace("\\", "/")))
                entries += 1
        for rel in DIRS:
            base = os.path.join(ROOT, rel)
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in BAD_DIR
                                 and d not in ("voices", "runtime")]
                for fn in filenames:
                    if fn.endswith((".pyc", ".log", ".db")):
                        continue
                    # v2.0.2：数据库的旁路文件（-wal/-shm/-journal）扩展名不是 .db，
                    # 上面那条拦不住；.env / .env.local 这类配置文件也必须挡住 ——
                    # 开源包里绝不能出现任何运行时数据或密钥。
                    _fl = fn.lower()
                    if _fl.endswith((".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")):
                        continue
                    if _fl == ".env" or _fl.startswith(".env.") or _fl.endswith((".key", ".pem")):
                        continue
                    # 备份文件、用户上传的参考音频、运行时缓存，都不该进发行包
                    if ".bak" in fn or fn.startswith("_.wav"):
                        continue
                        continue
                    # 开源包只放源码：av_server.exe（145MB）/ 驱动安装包都是构建产物，
                    # 源码里带着没意义还会把包撑到 145MB+（用户自己跑 build_av.py 生成）
                    if fn.lower().endswith((".exe", ".dll", ".msi", ".zip", ".7z")):
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(full) > 20 * 1048576:
                            continue
                    except OSError:
                        continue
                    arc = os.path.relpath(full, ROOT).replace("\\", "/")
                    z.write(full, "小鲸鱼_V%s/%s" % (version, arc))
                    entries += 1
        note = (
            "安全核心说明\n"
            "================\n\n"
            "小鲸鱼的安全核心不在源码包里。它被编译成 secure_core.pyc 随程序发布，\n"
            "目的是防止有人改一个字节就把「本机密钥加密盐」和「文件清单签名密钥」换掉 ——\n"
            "那会让老用户已保存的 API Key 全部失效、已发出的程序包签名全部对不上。\n\n"
            "如果你要二次开发，照着这四个接口自己重写一份 secure_core.py 即可，\n"
            "接口保持一致，程序就能跑：\n\n"
            "    secret_salt()          -> bytes  本机加密盐（API Key / 暗号）\n"
            "    manifest_key()         -> bytes  文件清单签名密钥\n"
            "    codeword_digest(text)  -> str    暗号指纹（不可逆，只用于比较）\n"
            "    version_tag()          -> str    诊断信息用的一小段标识\n\n"
            "拿到源码包后怎么跑：\n"
            "    1) pip install -r 依赖（见 Docs/开发者指南.md 或 README 的「从源码运行」）\n"
            "    2) python backend_server.py             # 只起后端 + 浏览器打开\n"
            "    3) cd desktop && npm install && npm run tauri dev   # 起带界面的桌面端\n"
        )
        z.writestr("小鲸鱼_V%s/安全核心说明.txt" % version, note)
        entries += 1

    with zipfile.ZipFile(zip_path) as zz:
        names = zz.namelist()
        got = len(names)
        bad = zz.testzip()
    if got != entries or bad:
        raise SystemExit("开源包自检失败：条目 %s（应为 %s），坏条目 %s" % (got, entries, bad))

    # v2.0.1（P0）：光数条目数不够 —— V2.0.0 的开源包条目数完全对，但 core/ 整个没进去，
    # 照样能过上面那一步。所以这里按「源码包必须能 import」来验：
    #   1) 顶层的 agent.py 要在；
    #   2) core/ 下每一个 .py 都要在，而且和 agent.py 是平级（同一个顶层目录里）；
    #   3) 拿到包的人第一步就是 `python -c "import agent"`，缺一个模块就直接 ModuleNotFoundError。
    prefix = "小鲸鱼_V%s/" % version
    need = [prefix + "agent.py"]
    core_dir = os.path.join(ROOT, "core")
    core_mods = []
    if os.path.isdir(core_dir):
        core_mods = sorted(n for n in os.listdir(core_dir) if n.endswith(".py"))
    for n in core_mods:
        need.append(prefix + "core/" + n)
    missing = [p for p in need if p not in names]
    if missing:
        raise SystemExit("开源包自检失败：缺少源码文件 %s（顶层前缀 %s）" % (missing, prefix))
    if len(core_mods) < 2:
        raise SystemExit("开源包自检失败：core/ 里只找到 %d 个模块，像是没打进包" % len(core_mods))
    log("[+] 开源包 -> %s（%s，%s 个条目）"
        % (os.path.basename(zip_path), human(os.path.getsize(zip_path)), got))
    return zip_path


def step_tools_zip(version):
    """v1.9.7.1：出「恢复工具.zip」，并把 P2 那条硬编码哈希核对一遍。

    P2 的修法是让 恢复官方安装.ps1 在启动时校验 verify_bundle.exe 的 sha256
    （复测报告 4.3：以前只有一句 Test-Path，谁把验签器换成「永远返回 0」的
    假货，整条验签链就白做了）。代价是 ps1 里多了一个跟着验签器变的常量，
    所以这里在打包时卡两道：

      1. ps1 必须带 UTF-8 BOM —— PowerShell 5.1 没有 BOM 会把中文读成乱码，
         当场语法错误（这坑 V1.9.7 踩过一次）；
      2. ps1 里写的 $verifyExeSha256 必须等于 Tools\\verify_bundle.exe 的实际哈希。

    对不上就直接打不出包，免得把「有人换过验签器」这种告警发到用户面前。
    """
    ps1 = os.path.join(TOOLS_DIR, "恢复官方安装.ps1")
    vbx = os.path.join(TOOLS_DIR, "verify_bundle.exe")
    for p in (ps1, vbx):
        if not os.path.isfile(p):
            raise SystemExit("恢复工具缺文件：%s" % p)
    raw = open(ps1, "rb").read()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("Tools\\恢复官方安装.ps1 少了 UTF-8 BOM —— PowerShell 5.1 会把中文"
                         "读成乱码、脚本当场语法错误。请用「UTF-8 带签名」重存一次再打包。")
    text = raw.decode("utf-8-sig")
    m = re.search(r'\$verifyExeSha256\s*=\s*"([0-9a-fA-F]{64})"', text)
    if not m:
        raise SystemExit("Tools\\恢复官方安装.ps1 里找不到 $verifyExeSha256（验签器自校验那段被删了？）")
    want = m.group(1).lower()
    got = sha256(vbx).lower()
    if want != got:
        raise SystemExit("验签器自校验的哈希对不上：\n"
                         "  ps1 里写的 %s\n  实际的     %s\n"
                         "把 Tools\\恢复官方安装.ps1 里的 $verifyExeSha256 改成实际值再打包。"
                         % (want, got))

    out = os.path.join(RELEASE_DIR, "小鲸鱼_V%s_恢复工具.zip" % version)
    if os.path.exists(out):
        os.remove(out)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in TOOLS_ZIP_FILES:
            p = os.path.join(TOOLS_DIR, name)
            if os.path.isfile(p):
                z.write(p, name)
                n += 1
    log("[+] 恢复工具 -> %s（%s，%d 个文件，验签器哈希已核对）"
        % (os.path.basename(out), human(os.path.getsize(out)), n))
    return out


def step_av_exe():
    """v1.3.2 终极修复：音视频微服务打成独立 av_server.exe。

    用户机器上没有 Python 时，av_bridge 会优先拉起这个 exe（自带运行时），
    所以它必须赶在 sync_av_module() 之前产出，才能被一起带进安装包。
    """
    script = os.path.join(ROOT, "build_av.py")
    if not os.path.isfile(script):
        log("    没有 build_av.py，跳过（音视频模块会退回 python 启动）")
        return
    run([sys.executable, "-X", "utf8", script], cwd=ROOT, env=build_env())


# v1.8.5：增量更新包自带的应用脚本（PowerShell / bat），出包时写进 zip
DELTA_PS1 = r'''
# 小鲸鱼 · 增量更新包 应用脚本（V1.8.6）
# 包内结构：payload\ 里才是要写进程序目录的文件；根目录只有清单、这个脚本、bat 和说明。
# 执行顺序：读清单 -> 校验 payload 里每个文件的哈希 -> 全部通过才允许动目标目录
#             -> 先备份将被覆盖/删除的旧文件 -> 再复制 -> 复制完再复核一遍哈希。
# 任何一步失败都会红字报错退出，目标目录一个字节都不动。
param(
    [string]$Target = "",
    [switch]$IgnoreRunning,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $here "payload"

function Fail($code, $msg, $detail) {
    Write-Host ""
    Write-Host ("  [失败] " + $msg) -ForegroundColor Red
    if ($detail) {
        foreach ($d in $detail) { Write-Host ("    - " + $d) -ForegroundColor Red }
    }
    Write-Host ""
    Write-Host "  目标目录没有被修改。" -ForegroundColor Yellow
    Write-Host ""
    exit $code
}

Write-Host ""
Write-Host "  小鲸鱼 · 增量更新" -ForegroundColor Cyan

$man = Join-Path $here "delta_manifest.json"
if (-not (Test-Path -LiteralPath $man)) {
    Fail 2 "没找到 delta_manifest.json —— 请把整个增量包解压出来再运行，不要只拷出 bat 文件。" $null
}
$j = $null
try { $j = Get-Content -Raw -Encoding UTF8 -LiteralPath $man | ConvertFrom-Json } catch { }
if (($null -eq $j) -or ($null -eq $j.files)) {
    Fail 2 "delta_manifest.json 读不出来，增量包可能不完整。" @("建议重新下载增量包")
}
if (-not (Test-Path -LiteralPath $Payload)) {
    Fail 2 "包里没有 payload 文件夹 —— 增量包不完整，请不要使用。" $null
}

# ---------- v1.9.3：先给自己验个身 ----------
# 已签名清单里记着 apply_delta.ps1 自己的 sha256。谁想把下面「验签」
# 那一段整块删掉再装点别的，改一个字节这里就对不上（复测报告 4.6）。
$selfPath = $MyInvocation.MyCommand.Path
$selfWant = ""
try {
    $prop = $j.scripts.PSObject.Properties['apply_delta.ps1']
    if ($null -ne $prop) { $selfWant = ([string]$prop.Value).Trim() }
} catch { }
if ([string]::IsNullOrWhiteSpace($selfWant)) {
    Fail 8 "清单里没有这个脚本的哈希 —— 这不是官方增量包，或者包被人改过。" @("请重新下载增量包")
}
$selfGot = ""
try {
    $selfGot = (Get-FileHash -Algorithm SHA256 -LiteralPath $selfPath).Hash
} catch {
    Fail 8 ("读不出自己的哈希：" + $selfPath) @("请把整个增量包重新解压一次")
}
if ($selfGot.ToLower() -ne $selfWant.ToLower()) {
    Fail 8 "这个更新脚本自己被改过（哈希对不上），不能运行。" @("请重新下载增量包，不要手工改 apply_delta.ps1")
}
Write-Host "  脚本自检：通过" -ForegroundColor DarkGray

# v1.8.11（任务八）：一键安装 —— 不给 -Target 就自己去找程序目录。
# 顺序：① 包所在目录往上 5 层 ② 桌面/开始菜单里叫「小鲸鱼」的快捷方式
#      ③ 常见安装位置 ④ 还找不到就停下来问人（可以直接把文件夹拖进窗口）
function Test-AppDir($p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $false }
    try { return (Test-Path -LiteralPath (Join-Path $p "小鲸鱼.exe")) } catch { return $false }
}

$found = @()
if ([string]::IsNullOrWhiteSpace($Target)) {
    $probe = $here
    for ($i = 0; $i -lt 5; $i++) {
        if (Test-AppDir $probe) { $found += $probe; break }
        $up = Split-Path -Parent $probe
        if ([string]::IsNullOrWhiteSpace($up) -or ($up -eq $probe)) { break }
        $probe = $up
    }
}
if (($found.Count -eq 0) -and [string]::IsNullOrWhiteSpace($Target)) {
    try {
        $sh = New-Object -ComObject WScript.Shell
        $dirs = @([Environment]::GetFolderPath('Desktop'),
                  [Environment]::GetFolderPath('CommonDesktopDirectory'),
                  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
        foreach ($d in $dirs) {
            if ([string]::IsNullOrWhiteSpace($d)) { continue }
            if (-not (Test-Path -LiteralPath $d)) { continue }
            $lnks = @(Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue)
            foreach ($l in $lnks) {
                if ($l.BaseName -notlike '*小鲸鱼*') { continue }
                try {
                    $tgt = $sh.CreateShortcut($l.FullName).TargetPath
                    if ($tgt) {
                        $td = Split-Path -Parent $tgt
                        if (Test-AppDir $td) { $found += $td; break }
                    }
                } catch { }
            }
            if ($found.Count -gt 0) { break }
        }
    } catch { }
}
if (($found.Count -eq 0) -and [string]::IsNullOrWhiteSpace($Target)) {
    $cand = @()
    foreach ($root in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:USERPROFILE)) {
        if ([string]::IsNullOrWhiteSpace($root)) { continue }
        $cand += (Join-Path $root "小鲸鱼")
        $cand += (Join-Path $root "Programs\小鲸鱼")
    }
    $cand += "C:\小鲸鱼"
    $cand += "D:\小鲸鱼"
    $cand += "E:\小鲸鱼"
    foreach ($c in $cand) { if (Test-AppDir $c) { $found += $c; break } }
}
if (($found.Count -gt 0) -and [string]::IsNullOrWhiteSpace($Target)) { $Target = $found[0] }

# 还找不到就问 —— 不会敲命令没关系，会拖文件夹就行
while ([string]::IsNullOrWhiteSpace($Target)) {
    Write-Host ""
    Write-Host "  没自动找到程序目录（有小鲸鱼.exe 的那一层）。" -ForegroundColor Yellow
    Write-Host "  把那个文件夹直接拖进这个窗口再回车就行；" -ForegroundColor Yellow
    Write-Host "  也可以右键桌面的「小鲸鱼」快捷方式 → 打开文件所在的位置，复制上面地址栏的路径贴进来。" -ForegroundColor Yellow
    Write-Host ""
    $in = Read-Host "  程序目录（直接回车放弃）"
    if ([string]::IsNullOrWhiteSpace($in)) { break }
    $in = $in.Trim().Trim([char]34).Trim([char]39)
    if (Test-AppDir $in) { $Target = $in }
    else { Write-Host ("  这个目录里没有 小鲸鱼.exe：" + $in) -ForegroundColor Red }
}
if ([string]::IsNullOrWhiteSpace($Target)) {
    Fail 6 "还是没有可用的程序目录。" @(
        ("本脚本现在在：" + $here)
        "用法一（最省事）：把整个包解压进程序目录本身（跟 小鲸鱼.exe 放一起），再双击 应用增量包.bat"
        ("用法二：powershell -NoProfile -ExecutionPolicy Bypass -File apply_delta.ps1 -Target " + [char]34 + "<程序目录>" + [char]34)
        "不知道怎么找程序目录：右键桌面上的「小鲸鱼」快捷方式 → 打开文件所在的位置"
    )
}
if (-not (Test-Path -LiteralPath $Target)) { Fail 2 ("目标目录不存在：" + $Target) $null }
$Target = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\')
$PayloadReal = (Resolve-Path -LiteralPath $Payload).Path.TrimEnd('\')

Write-Host ("  包内版本：{0}  ->  {1}" -f $j.from_version, $j.to_version)
Write-Host ("  程序目录：{0}" -f $Target)
Write-Host ""

# ---------- v2.0.0：跨大版本升级硬拦截 ----------
# 为什么必须拦：V1.9.x → V2.0.0 是底层结构变更（单文件 agent.py 拆成 core\ 九个模块）。
# 增量包只换「变了的那几个文件」，跨代套用会留下半旧半新的混合目录 —— 壳的清单校验
# 对不上，程序直接判「被篡改」，用户看到的就是「更新完打不开」，只能拿完整包重装。
# 与其让人升成半砖再返工，不如在这里先红字拦住，一个字节都不动。
function Get-InstalledVersion([string]$dir) {
    # 目录为空时直接返回：脚本里 $ErrorActionPreference = "Stop"，
    # 空路径会让 Join-Path 抛异常，把「读不到版本」变成「脚本崩掉」。
    if ([string]::IsNullOrWhiteSpace($dir)) { return "" }
    # 先看后端 exe 的版本资源（两种布局都有，且 V1.9.x 也带）
    foreach ($rel in @("resources\backend_server.exe", "backend_server.exe")) {
        $p = ""
        try { $p = Join-Path $dir $rel } catch { continue }
        if ([string]::IsNullOrWhiteSpace($p)) { continue }
        if (-not (Test-Path -LiteralPath $p)) { continue }
        try {
            $vi = (Get-Item -LiteralPath $p).VersionInfo
            $v = [string]$vi.ProductVersion
            if ([string]::IsNullOrWhiteSpace($v)) { $v = [string]$vi.FileVersion }
            if (-not [string]::IsNullOrWhiteSpace($v)) { return $v.Trim() }
        } catch { }
    }
    return ""
}

$curVer = Get-InstalledVersion $Target
$toMajor = 0
if (([string]$j.to_version) -match '^\s*(\d+)') { $toMajor = [int]$Matches[1] }
$curMajor = -1
if ($curVer -match '^\s*(\d+)') { $curMajor = [int]$Matches[1] }
if ($curMajor -ge 0 -and $toMajor -gt 0 -and $curMajor -ne $toMajor) {
    Write-Host ""
    Write-Host "  ✗ 检测到跨大版本升级，底层结构已变更。" -ForegroundColor Red
    Write-Host ("    当前版本：{0}    这个增量包：{1}" -f $curVer, $j.to_version) -ForegroundColor Red
    Write-Host "    请下载最新的【完整安装包】或【完整绿色版】覆盖，" -ForegroundColor Red
    Write-Host "    本增量包不支持此操作。" -ForegroundColor Red
    Write-Host "    （覆盖安装 / 覆盖解压不会丢聊天记录和记忆）" -ForegroundColor Yellow
    Write-Host ""
    Fail 7 "跨大版本升级被拦下（目标目录一个字节都没动）。" @(
        ("请用 V" + $j.to_version + " 的完整安装包覆盖安装"),
        "只想要绿色版：下载对应版本的【完整绿色版】解压覆盖",
        "增量包只支持同一大版本内的小版本升级"
    )
}
if ([string]::IsNullOrWhiteSpace($curVer)) {
    Write-Host "  当前版本：读不出来（按同一大版本继续，校验会兜底）" -ForegroundColor Yellow
} else {
    Write-Host ("  当前版本：{0}" -f $curVer) -ForegroundColor DarkGray
}

# ---------- v1.9.7.1：先看这是「安装版」还是「绿色版」布局 ----------
# 安装包装出来的目录，清单在 resources\ 里；绿色版解压出来的，清单在程序目录里。
# 两者的主程序只差 3 个字节（NSIS 打包器把包类型标记 UNK 改成了 NSS），
# 所以清单内容也不一样 —— 包里带了两份，这里按布局挑一份，绝不混用。
$manRoot = Join-Path $Target "manifest.sha256"
$manRes = Join-Path $Target (Join-Path "resources" "manifest.sha256")
$installLayout = $false
if (Test-Path -LiteralPath $manRes) { $installLayout = $true }
if ($installLayout) {
    Write-Host "  布局：安装版（清单在 resources\ 里）" -ForegroundColor DarkGray
    if (-not $j.install_layout) {
        Fail 2 "这个增量包是给绿色版准备的，里面没有安装版的清单。" @(
            "请用完整安装包升级一次（覆盖安装不会丢数据）",
            "或者从官网下载 V$($j.to_version) 的安装包"
        )
    }
    # 安装包里的主程序和绿色版的主程序字节不同（打包器改了 3 个字节），
    # 所以安装版必须换「安装形态」那一份：包里带的是 payload/install/小鲸鱼.exe。
    # 老流程打的包只带绿色版那份，硬盖上去会自判篡改 —— 那种情况停下说清楚。
    $needExe = @($j.files | Where-Object { $_.path -eq "小鲸鱼.exe" }).Count -gt 0
    $haveExe = @($j.install_layout.files | Where-Object { ([string]$_.dest) -eq "小鲸鱼.exe" }).Count -gt 0
    if ($needExe -and (-not $haveExe)) {
        Fail 2 "这个增量包里带了主程序，却没带「安装版的那一份」，装上会自判篡改。" @(
            "请用 V$($j.to_version) 的完整安装包覆盖安装（数据不会丢）",
            "绿色版用户可以直接双击「应用增量包.bat」"
        )
    }
} else {
    Write-Host "  布局：绿色版（清单在程序目录里）" -ForegroundColor DarkGray
}

# 源 = 目标：真出现就是自己覆盖自己，必须提前挡住
if ($Target -eq $PayloadReal) {
    Fail 5 "目标目录和包里的 payload 是同一个目录，这样等于自己复制给自己。" @("请把增量包解压到任意位置（不要选 payload 这一层）", "例如：apply_delta.ps1 -Target D:\小鲸鱼")
}
if ($Target.StartsWith($PayloadReal + "\")) {
    Fail 5 "目标目录在 payload 里面，方向反了。" @("请用 -Target 指向程序目录（有 小鲸鱼.exe 的那一层）")
}
if ($PayloadReal.StartsWith($Target + "\")) {
    Write-Host "  提醒：你把增量包解压进了程序目录本身。这次会正常更新完，下次更推荐解压到别处再用 -Target。" -ForegroundColor Yellow
}

# 目标得像个小鲸鱼目录，否则容易写错地方
if (-not $Force) {
    if (-not (Test-Path -LiteralPath (Join-Path $Target "小鲸鱼.exe"))) {
        Fail 6 ("这个目录里没有 小鲸鱼.exe，看着不像程序目录：" + $Target) @("确认目录没错的话，加 -Force 再跑一次")
    }
}

# ---------- v1.9.7.1（P1）：只认「这个程序目录里」的进程 ----------
# 以前这里按进程名一刀切。双版本并存时（测试版 + 正式版各装一套）更新其中一套，
# 会把另一套一起关掉，而那套可能正在写数据库 —— 这是真实事故（复测报告 6.2）。
# 判据跟「恢复官方安装」工具一致：进程的可执行文件路径以 <目标目录>\ 开头才算。
function Get-TargetProcs {
    $names = @("小鲸鱼", "backend_server", "av_server")
    $found = @()
    foreach ($pr in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            if ($names -notcontains $pr.ProcessName) { continue }
        } catch { continue }
        $pp = $null
        try { $pp = $pr.Path } catch { $pp = $null }
        if ([string]::IsNullOrWhiteSpace($pp)) { continue }
        if ($pp.StartsWith($Target + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            $found += $pr
        }
    }
    return $found
}

# 程序开着的时候 exe 是锁的，写不进去 —— 先说清楚，别让用户以为更新成功了
if (-not $IgnoreRunning) {
    $busy = @(Get-TargetProcs)
    if ($busy.Count -gt 0) {
        $bn = (@($busy | ForEach-Object { $_.ProcessName }) | Select-Object -Unique) -join "、"
        Fail 3 ("请先关掉这些程序再运行：" + $bn) @("托盘图标右键退出，或任务管理器里结束", "只关这个目录里的（别的安装不受影响）", "确认已经关掉了，加 -IgnoreRunning 再跑一次")
    }
}

# ---------- 0) v1.9.1：先验签，再谈别的 ----------
# 清单里的 sha256 只能证明「包没下坏」，证明不了「包是我们发的」——
# 谁都能换掉 payload 再重算清单。清单本身带 Ed25519 签名（发布公钥写死在程序里，
# 长期不变），PowerShell 没有原生 Ed25519，只能借本机上的程序来验。
# v1.9.2（N-02）：一定要用**目标目录里已安装的**那份，不能用 payload 里那份 ——
# 拿嫌疑人自己出示的无罪证明不算验证。这里优先用已安装的 backend_server.exe
# （控制台程序，退出码可靠）；它不在时才退回主程序（GUI 程序没有控制台，
# PowerShell 里 $LASTEXITCODE 会是 null，所以让它把结论写进临时文件）。
function Test-VerifyDeltaSupport($exe) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes($exe)
        $txt = [System.Text.Encoding]::ASCII.GetString($bytes)
        return $txt.Contains("--verify-delta")
    } catch {
        return $false
    }
}

$verBackend = Join-Path (Join-Path $Target "resources") "backend_server.exe"
$verExe = Join-Path $Target "小鲸鱼.exe"
$sigOut = @()
$sigRc = 4
$sigHow = ""
# 验签程序会自己建数据目录（runtime\api_token.txt）——不能让它在目标目录里
# 留东西，否则「校验失败、目标目录零改动」这句话就不成立了。
$sigDataOld = $env:QQ_AGENT_DATA_DIR
$sigDataTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("xjy-verify-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$env:QQ_AGENT_DATA_DIR = $sigDataTmp
# 这个目录必须先建出来：Start-Process 的 -WorkingDirectory 指向不存在的目录会
# 直接报 "parameter WorkingDirectory has a value that is not valid"，验签就永远失败。
if (-not (Test-Path -LiteralPath $sigDataTmp)) { New-Item -ItemType Directory -Path $sigDataTmp -Force | Out-Null }

    if (Test-Path -LiteralPath $verBackend) {
        $sigHow = "已安装的后端"
        Write-Host "  验签：用目标目录里已安装的后端（不是包里那份），起在临时目录里跑" -ForegroundColor DarkGray
        # v1.9.4（抢修一）：用 Start-Process 拿到进程对象，跑完必须等它真的退出。
        # 以前是 & 直接调，控制台程序虽然也会等，但拿不到进程句柄、也没法定增超时；
        # 万一它卡住不退，后面替换文件就会被文件锁挡着 —— 半升级就是这么来的。
        $sigProc = $null
        try {
            $sigProc = Start-Process -FilePath $verBackend -ArgumentList @("--verify-delta", $here) -WorkingDirectory $sigDataTmp -PassThru -Wait -WindowStyle Hidden
            if ($null -eq $sigProc -or $null -eq $sigProc.ExitCode) { $sigRc = 4 } else { $sigRc = [int]$sigProc.ExitCode }
        } catch {
            $sigRc = 4
            $sigOut = @("$($_.Exception.Message)")
        }
        if ($null -ne $sigProc -and -not $sigProc.HasExited) {
            try { $sigProc.WaitForExit(60000) | Out-Null } catch { }
            if (-not $sigProc.HasExited) { try { $sigProc.Kill() } catch { }; $sigRc = 4; $sigOut = @("验签程序 60 秒还没退出，已强制结束") }
        }
        $sigProc = $null
        # 再确认一遍：目标目录里没有残留的 backend_server 占着文件
        # v1.9.7.1（P1）：只看目标目录里的进程。以前按名字扫，会把另一套安装的
        # backend_server 一起等 30 秒、再一起 Kill —— 那套可能正在写数据库。
        $sigLeft = @(Get-TargetProcs)
        if ($sigLeft.Count -gt 0) {
            try { $sigLeft | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue } catch { }
            foreach ($sp in @(Get-TargetProcs)) {
                try { $sp.WaitForExit(10000) | Out-Null } catch { }
                if (-not $sp.HasExited) { try { $sp.Kill(); $sp.WaitForExit(5000) | Out-Null } catch { } }
            }
        }
} elseif ((Test-Path -LiteralPath $verExe) -and (Test-VerifyDeltaSupport $verExe)) {
        $sigHow = "主程序"
        $resFile = Join-Path ([System.IO.Path]::GetTempPath()) ("xjy-vd-" + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".txt")
        $sigProc2 = $null
        try {
            Remove-Item -LiteralPath $resFile -Force -ErrorAction SilentlyContinue
            $sigProc2 = Start-Process -FilePath $verExe -ArgumentList @("--verify-delta", $here, "--result-file", $resFile) -WorkingDirectory $sigDataTmp -PassThru -Wait -WindowStyle Hidden
            for ($i = 0; $i -lt 120; $i++) {
                if (Test-Path -LiteralPath $resFile) { break }
                Start-Sleep -Milliseconds 500
            }
            if ($null -ne $sigProc2 -and -not $sigProc2.HasExited) {
                try { $sigProc2.WaitForExit(30000) | Out-Null } catch { }
                if (-not $sigProc2.HasExited) { try { $sigProc2.Kill() } catch { } }
            }
            if (Test-Path -LiteralPath $resFile) {
                $txt = @(Get-Content -LiteralPath $resFile -Encoding UTF8)
                try {
                    $sigRc = [int]("$($txt[0])".Trim())
                } catch {
                    $sigRc = 4
                }
                if ($txt.Count -gt 1) { $sigOut = $txt[1..($txt.Count - 1)] } else { $sigOut = @() }
            } else {
                $sigRc = 4
                $sigOut = @("验签程序没有返回结果（等了 60 秒）")
            }
            Remove-Item -LiteralPath $resFile -Force -ErrorAction SilentlyContinue
        } catch {
            $sigRc = 4
            $sigOut = @("$($_.Exception.Message)")
        }
        $sigProc2 = $null
} else {
        if ($sigDataOld) {
            $env:QQ_AGENT_DATA_DIR = $sigDataOld
        } else {
            Remove-Item Env:\QQ_AGENT_DATA_DIR -ErrorAction SilentlyContinue
        }
        Fail 7 ("找不到能验签的程序，没法安全地更新：" + $Target) @(
            "这个目录里既没有 resources\backend_server.exe，也没有 小鲸鱼.exe"
            "请用完整安装包升级一次，之后的增量包就能正常用了"
        )
}


if ($sigDataOld) {
    $env:QQ_AGENT_DATA_DIR = $sigDataOld
} else {
    Remove-Item Env:\QQ_AGENT_DATA_DIR -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $sigDataTmp -Recurse -Force -ErrorAction SilentlyContinue
foreach ($line in $sigOut) { if ("$line" -match "\S") { Write-Host ("  " + "$line") -ForegroundColor DarkGray } }
if ($sigRc -ne 0) {
    Fail 4 ("增量包签名校验没通过（用" + $sigHow + "验的）—— 这个包不是官方发的，或者被改过。") @(
        "请从官方渠道重新下载增量包",
        "如果反复失败，直接用完整安装包覆盖安装"
    )
}


# ---------- 1) 先校验，全部通过才允许动目标目录 ----------
$items = @()
$bad = @()
$drift = 0
foreach ($f in $j.files) {
    $rel = [string]$f.path
    if ($installLayout -and ($rel -eq "manifest.sha256" -or $rel -eq "manifest.sig" -or $rel -eq "小鲸鱼.exe")) {
        # 安装版布局不用绿色版这份清单，主程序也要换成安装形态那一份（下面单独放）
        continue
    }
    if ([string]::IsNullOrWhiteSpace($rel) -or $rel.Contains("..") -or $rel.StartsWith("/") -or $rel.StartsWith("\") -or ($rel -match "^[A-Za-z]:")) {
        $bad += ($rel + "（清单里的路径不合法，拒绝写入）")
        continue
    }
    $src = Join-Path $Payload ($rel -replace "/", "\")
    if (-not (Test-Path -LiteralPath $src)) {
        $bad += ($rel + "（payload 里缺这个文件）")
        continue
    }
    $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $src).Hash.ToLower()
    if ($got -ne ([string]$f.sha256).ToLower()) {
        $bad += ($rel + "（哈希不对，包可能没下完或被改过）")
        continue
    }
    $items += [pscustomobject]@{ rel = $rel; src = $src; sha = $got }
    if ($f.base_sha256) {
        $dstTry = Join-Path $Target ($rel -replace "/", "\")
        if (Test-Path -LiteralPath $dstTry) {
            # v1.9.4：目标文件正被占用时这里读不动。读不到就当「对不上」，
        # 继续往下走 —— 真正要拦的是替换阶段，那里有回滚兜着。
        $cur = ""
        try { $cur = (Get-FileHash -Algorithm SHA256 -LiteralPath $dstTry).Hash.ToLower() } catch { $cur = "" }
            if ($cur -ne ([string]$f.base_sha256).ToLower()) { $drift++ }
        }
    }
}

# 安装版布局：清单要落到 resources\ 里，内容也换成按安装包内主程序算的那一份
if ($installLayout) {
    foreach ($f in $j.install_layout.files) {
        $rel = [string]$f.path
        $dstRel = ([string]$f.dest) -replace "/", "\"
        if ([string]::IsNullOrWhiteSpace($rel) -or $rel.Contains("..") -or [string]::IsNullOrWhiteSpace($dstRel)) {
            $bad += ("清单里安装版条目路径不合法，拒绝写入")
            continue
        }
        $src = Join-Path $Payload ($rel -replace "/", "\")
        if (-not (Test-Path -LiteralPath $src)) {
            $bad += ($dstRel + "（payload 里缺这个文件）")
            continue
        }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $src).Hash.ToLower()
        if ($got -ne ([string]$f.sha256).ToLower()) {
            $bad += ($dstRel + "（哈希不对，包可能没下完或被改过）")
            continue
        }
        $items += [pscustomobject]@{ rel = $dstRel; src = $src; sha = $got }
    }
    # 万一程序目录里还留着一份旧的根清单（以前版本放错了），一并清掉：
    # 壳的规则是「附近每一份清单都必须通过」，多留一份旧的就会自判篡改。
    foreach ($dn in @("manifest.sha256", "manifest.sig")) {
        if ((Test-Path -LiteralPath (Join-Path $Target $dn)) -and (-not (@($j.remove) -contains $dn))) {
            $j.remove = @($j.remove) + @($dn)
        }
    }
}
if ($bad.Count -gt 0) {
    Fail 4 "校验没通过，一个文件都没动：" $bad
}
Write-Host ("  校验通过（{0} 个文件）" -f $items.Count) -ForegroundColor Green
if ($drift -gt 0) {
    Write-Host ("  注意：有 {0} 个文件和当前版本对不上（自己改过？）—— 照常覆盖，旧文件都会备份。" -f $drift) -ForegroundColor Yellow
}

# v1.9.9 修：函数必须先定义再调用（PowerShell 不会把后面的定义提前）
function Invoke-Rollback([string]$why) {
    Write-Host ""
    Write-Host "  [回滚] 正在把程序恢复成更新前的样子……" -ForegroundColor Yellow
    $rp = ""
    if (-not [string]::IsNullOrWhiteSpace($backup)) { $rp = Join-Path $backup "restore.ps1" }
    $ok = $true
    if (Test-Path -LiteralPath $rp) {
        try {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $rp 2>&1 | Out-Host
        } catch { }
        # 别拿 restore.ps1 的退出码当结论：它碰到「正被占着的文件」会报错退出，
        # 但那个文件本来就没被改动过，内容其实是对的。以哈希为准。
        $ok = $true
        $diff = 0
        foreach ($f in @(Get-ChildItem -LiteralPath $backup -Recurse -File -ErrorAction SilentlyContinue)) {
            if ($f.Name -eq "restore.ps1" -or $f.Name -eq "added.txt") { continue }
            $rel2 = $f.FullName.Substring($backup.Length).TrimStart("\")
            $dst2 = Join-Path $Target $rel2
            if (-not (Test-Path -LiteralPath $dst2)) { $diff++; continue }
            $cur2 = ""
            try { $cur2 = (Get-FileHash -Algorithm SHA256 -LiteralPath $dst2).Hash.ToLower() } catch { $cur2 = "" }
            # 读不动 = 正被别的程序占着：这种文件既换不掉也丢不了，跳过不算差异
            if ($cur2 -eq "") { continue }
            $want2 = ""
            try { $want2 = (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash.ToLower() } catch { $want2 = "" }
            if ($cur2 -ne $want2) { $diff++ }
        }
        if ($diff -gt 0) { $ok = $false }
    } elseif ($done -gt 0) {
        # 这次更新全是新增文件（没有旧文件要备份），那就把新写进去的删掉。
        # 注意 $done：还没开始替换时这里一个文件都不该碰 —— $items 里的路径
        # 在目标目录里可能本来就有（备份失败那种情况），误删就是删用户的程序。
        foreach ($it in $items) {
            $dst = Join-Path $Target ($it.rel -replace "/", "\")
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($tmpDir)) {
        if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
    $where = $backup
    if ([string]::IsNullOrWhiteSpace($where)) { $where = "（当时还没开始备份）" }
    if ($saved -eq 0) { $where = "（这次没有旧文件被覆盖，新增的文件已经删掉了）" }
    if ($ok) {
        Write-Host "  已经回到更新前的状态。" -ForegroundColor Green
    } else {
        Write-Host "  自动回滚没能完整跑完 —— 请手动双击这个文件：" -ForegroundColor Red
        Write-Host ("    " + $rp) -ForegroundColor Yellow
    }
    Write-Host ("  备份位置：" + $where) -ForegroundColor Yellow
    Write-Host "  如果还是打不开，直接下完整安装包覆盖安装一遍就好，数据不会丢。" -ForegroundColor Yellow
    Write-Host ""
    $m = "更新没有完成，已经自动退回更新前的版本。"
    if (-not $ok) { $m = "更新没有完成，自动回滚也没能跑完。请双击备份目录里的 restore.ps1，或者直接下载完整安装包覆盖安装。" }
    $m = $m + "`r`n`r`n原因：" + $why
    $m = $m + "`r`n`r`n备份：" + $where
    try { (New-Object -ComObject WScript.Shell).Popup($m, 25, "小鲸鱼 · 更新失败", 16) | Out-Null } catch { }
    exit 9
}

# ---------- 2) 先备份将被覆盖/删除的旧文件 ----------
# v1.9.4（抢修一）：这一步也会失败 —— 目标目录里的 exe 正被别的程序占着时
# Get-FileHash 读不动。以前这会让脚本以退出码 1 猝死，用户既看不懂也不知道
# 该干什么；现在统一走 Invoke-Rollback（此时还没动过任何文件，回滚只是收尾）。
$backup = ""
$tmpDir = ""
try {
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $Target ("_backup\pkg-" + $stamp)
$saved = 0
$added = @()
$removed = @()
foreach ($it in $items) {
    $dst = Join-Path $Target ($it.rel -replace "/", "\")
    if (Test-Path -LiteralPath $dst) {
        $bak = Join-Path $backup ($it.rel -replace "/", "\")
        $bdir = Split-Path -Parent $bak
        if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        Copy-Item -LiteralPath $dst -Destination $bak -Force
        $saved++
    } else {
        $added += $it.rel
    }
}
foreach ($r in $j.remove) {
    $rel = [string]$r
    if ([string]::IsNullOrWhiteSpace($rel) -or $rel.Contains("..")) { continue }
    $dst = Join-Path $Target ($rel -replace "/", "\")
    if (Test-Path -LiteralPath $dst) {
        $bak = Join-Path $backup ($rel -replace "/", "\")
        $bdir = Split-Path -Parent $bak
        if (-not (Test-Path -LiteralPath $bdir)) { New-Item -ItemType Directory -Force -Path $bdir | Out-Null }
        Copy-Item -LiteralPath $dst -Destination $bak -Force
        $saved++
        $removed += $rel
    }
}

if ($saved -gt 0) {
    if (-not (Test-Path -LiteralPath $backup)) { New-Item -ItemType Directory -Force -Path $backup | Out-Null }
    $restoreText = @'
$ErrorActionPreference = 'Stop'
$bak = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Split-Path -Parent (Split-Path -Parent $bak)
Write-Host ("把 " + $bak + " 里的文件写回 " + $target) -ForegroundColor Cyan
Get-ChildItem -Recurse -File -LiteralPath $bak | ForEach-Object {
    if ($_.Name -eq 'restore.ps1') { return }
    if ($_.Name -eq 'added.txt') { return }
    $rel = $_.FullName.Substring($bak.Length).TrimStart('\')
    $dst = Join-Path $target $rel
    $d = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
}
$addf = Join-Path $bak 'added.txt'
if (Test-Path -LiteralPath $addf) {
    Get-Content -Encoding UTF8 -LiteralPath $addf | ForEach-Object {
        $rel = $_.Trim()
        if ($rel) {
            $dst = Join-Path $target ($rel -replace '/', '\')
            if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue }
    }
    }
}
Write-Host '已回滚到更新前的状态。' -ForegroundColor Green
'@
    Set-Content -LiteralPath (Join-Path $backup "restore.ps1") -Value $restoreText -Encoding UTF8
    if ($added.Count -gt 0) { Set-Content -LiteralPath (Join-Path $backup "added.txt") -Value ($added -join "`r`n") -Encoding UTF8 }
    Write-Host ("  已备份 {0} 个旧文件到：{1}" -f $saved, $backup)
}
} catch {
    $why = "$($_.Exception.Message)"
    Write-Host ""
    Write-Host ("  [复制失败] " + $why) -ForegroundColor Red
    Write-Host "  备份没做完（多半是文件正被别的程序占着），还没动过目标目录。" -ForegroundColor Yellow
    Invoke-Rollback $why
}

# ---------- v1.9.4（抢修一）：出错就往回退 ----------
# 只要后面的步骤有任何一步失败，立刻把备份写回去，然后在屏幕上弹一个框。
# 半升级（exe 换了、后端没换）比不升级更糟：程序起不来，用户也不知道该怎么办。


# ---------- 3) v1.9.4：先搬进暂存区，再一次性换上 ----------
# 以前是逐个文件直接往目标目录里复制，中途断掉（盘满、杀软锁文件、强制关机）
# 就会停在「一半新一半旧」——QA 说的半升级变砖就是这个。
# 现在分三步走，任何一步出事都能退回去：
#   ① 全部复制进 <程序目录>\_update_tmp 并逐个复核哈希（这一步还没碰旧文件）
#   ② 用 Move-Item -Force 在同一块盘上重命名覆盖（每一下都是原子的）
#   ③ 复核；不对就抛错，由上面的 catch 自动回滚
$tmpDir = Join-Path $Target "_update_tmp"
$done = 0
$postbad = @()
try {
    if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

    # 3.1 备到暂存区（目标目录此时一个字节都没动）
    foreach ($it in $items) {
        $stage = Join-Path $tmpDir ($it.rel -replace "/", "\")
        $sdir = Split-Path -Parent $stage
        if (-not (Test-Path -LiteralPath $sdir)) { New-Item -ItemType Directory -Force -Path $sdir | Out-Null }
        Copy-Item -LiteralPath $it.src -Destination $stage -Force -ErrorAction Stop
        $chk = (Get-FileHash -Algorithm SHA256 -LiteralPath $stage).Hash.ToLower()
        if ($chk -ne $it.sha) { throw ("暂存后哈希不对：" + $it.rel) }
    }
    Write-Host ("  已备好 {0} 个新文件（暂存在 _update_tmp 里）" -f $items.Count) -ForegroundColor DarkGray

    # 3.2 统一替换：同一块盘上的移动 = 改名，单个文件是原子的
    foreach ($it in $items) {
        $stage = Join-Path $tmpDir ($it.rel -replace "/", "\")
        $dst = Join-Path $Target ($it.rel -replace "/", "\")
        $ddir = Split-Path -Parent $dst
        if (-not (Test-Path -LiteralPath $ddir)) { New-Item -ItemType Directory -Force -Path $ddir | Out-Null }
        Move-Item -LiteralPath $stage -Destination $dst -Force -ErrorAction Stop
        $done++
    }
    foreach ($rel in $removed) {
        $dst = Join-Path $Target ($rel -replace "/", "\")
        if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue }
    }

    # ---------- 4) 换完再复核一遍，别谎报成功 ----------
    foreach ($it in $items) {
        $dst = Join-Path $Target ($it.rel -replace "/", "\")
        $chk = (Get-FileHash -Algorithm SHA256 -LiteralPath $dst).Hash.ToLower()
        if ($chk -ne $it.sha) { $postbad += $it.rel }
    }
    if ($postbad.Count -gt 0) {
        throw ("有 {0} 个文件换上之后哈希还是不对：{1}" -f $postbad.Count, ($postbad -join "、"))
    }
} catch {
    $why = "$($_.Exception.Message)"
    Write-Host ""
    Write-Host ("  [失败] " + $why) -ForegroundColor Red
    Write-Host ("  已经换了 {0} 个文件，现在自动回滚" -f $done) -ForegroundColor Yellow
    Invoke-Rollback $why
}
if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host ("  完成：写入 {0} 个文件，删掉 {1} 个" -f $done, $removed.Count) -ForegroundColor Green
if ($saved -gt 0) { Write-Host ("  旧文件备份在：{0}" -f $backup) }
Write-Host ("  现在双击 小鲸鱼.exe 就是 V{0} 了。" -f $j.to_version)
Write-Host ""

'''

DELTA_BAT = [
	"@echo off",
	"chcp 65001 >nul",
	"title 小鲸鱼 · 增量更新（一键）",
	"rem 小鲸鱼 · 增量更新包（V1.8.11）：双击这个就行 —— 它会自己去找程序目录",
	"echo.",
	"echo   小鲸鱼 · 增量更新",
	"echo   ------------------------------",
	"echo   这个窗口会做三件事：",
	"echo     1. 自己找小鲸鱼装在哪（找不到会问你）",
	"echo     2. 校验包里的文件有没有损坏",
	"echo     3. 备份旧文件，再换上新文件",
	"echo.",
	"powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0apply_delta.ps1\" %*",
	"echo.",
	"echo   窗口不会自动关，看完再按任意键。",
	"pause",
]

# v1.8.6：增量包根目录的 README（告诉用户 payload/ 是干嘛的）
DELTA_README = r'''
# 小鲸鱼 · 增量更新包

这个包里**只有变过的文件**（这次是 3 个），不用重下 420MB 的整包。

## 包里有什么

| 名字 | 说明 |
|---|---|
| `应用增量包.bat` | 双击这个就行 |
| `apply_delta.ps1` | 真正的更新脚本（bat 会调它） |
| `delta_manifest.json` | 清单：哪些文件、多大、SHA256、从哪个版本到哪个版本 |
| `payload\` | **真正要写进程序目录的文件都在这里面** |

## 怎么用

先把小鲸鱼退干净（托盘图标右键退出；`backend_server.exe` 也要退掉），然后二选一：

**用法一（推荐，最省事）：解压进程序目录本身，再双击**

1. 把这个 zip **直接解压进程序目录里**，跟 `小鲸鱼.exe` 放在一起。
2. 双击 `应用增量包.bat`。
3. 脚本会认出「包就在程序目录里」，黄字提醒一句，然后照常更新完。

**一键安装（v1.8.11 起，最省事）**

把 zip 解压到**任何地方**都行（桌面、下载文件夹），然后**双击 `应用增量包.bat`** —— 它会自己去找程序目录：

1. 先看包里这层和往上 5 层有没有 `小鲸鱼.exe`；
2. 再翻桌面和开始菜单里叫「小鲸鱼」的快捷方式，顺着快捷方式找过去；
3. 再看几个常见安装位置（`%LOCALAPPDATA%\Programs\小鲸鱼`、`C:\小鲸鱼`、`D:\小鲸鱼` 等）；
4. 实在找不到，它会停下来问你 —— **把程序文件夹直接拖进那个黑窗口，按回车就行**。

**用法二：解压到任意位置，用命令行指定程序目录**

1. 把 zip 解压到哪儿都行（桌面、下载文件夹）。
2. 在那个文件夹里打开命令行，运行：

```
powershell -NoProfile -ExecutionPolicy Bypass -File apply_delta.ps1 -Target "<程序目录>"
```

   不带 `-Target` 时，脚本会从自己所在目录**往上找 4 层**，哪层有 `小鲸鱼.exe` 就更新哪层；
   4 层都找不到就红字退出，并告诉你该怎么指定。

**最后一步**：看窗口里出现绿字「完成」就成功了，双击 `小鲸鱼.exe` 就是新版本。

## 它会做什么、不会做什么

- **先校验再动手**：把 `payload\` 里每个文件算一遍 SHA256，跟清单对不上就红字退出，**目标目录一个字节都不会动**。
- **先备份再覆盖**：要覆盖的旧文件先复制到 `<程序目录>\_backup\pkg-<时间戳>\`，同时写一个 `restore.ps1`（双击就能退回更新前）。
- **换得上就换、换不上就退**（v1.9.4）：新文件先全部搬到 `<程序目录>\_update_tmp`，确认无误后才逐个改名顶替；中途任何一步失败会**自动回滚**，并弹窗告诉你原因，不会把你留在「一半新一半旧」的状态。
- **复制完复核**：写完再算一遍哈希，不对就报错并告诉你怎么回滚，绝不谎报成功。
- **不动你的数据**：`config.json`、`memory.db`、`personas.json`、`stickers\`、`themes\` 这些一个都不碰（它们本来也不在包里）。
- **认得出「解压错了地方」**：目标目录正好是 `payload\` 本身会直接拒绝（退出码 5）；把包解压进程序目录本身是**允许**的（黄字提醒一句后照常更新完），不会死循环也不会崩。
- **认错目录**：目标目录里没有 `小鲸鱼.exe` 就报错退出（确认没错可以加 `-Force`）。

## 出错了怎么办

| 退出码 | 意思 |
|---|---|
| 2 | 找不到/读不出 `delta_manifest.json`，或没有 `payload\` —— 包不完整，重新下载 |
| 3 | 小鲸鱼 / backend_server / av_server 还开着，先关掉（或加 `-IgnoreRunning`） |
| 4 | payload 里文件哈希不对 —— 包下载不完整或被改过，**什么都没动**，重新下载 |
| 5 | 目标目录和包内 `payload\` 是同一个目录 / 目标在 payload 里面 |
| 6 | 目标目录里没有 `小鲸鱼.exe`，不像程序目录 |
| 7 | 写完之后复核哈希失败 —— 用备份目录里的 `restore.ps1` 回滚 |
| 8 | 更新脚本自己被改过（哈希对不上）—— 重新下载，别手改 `apply_delta.ps1` |
| 9 | 换文件途中失败 —— **已经自动回滚**到更新前的状态，按弹窗提示处理 |

实在不行就下整包（绿色版 zip 或安装包），那个不需要这个脚本。

'''


def _zip_index(path):
    """{条目名: (sha256, 大小)} —— 用来对比两个包里同名文件有没有变。"""
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            h = hashlib.sha256()
            with z.open(info) as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[info.filename.replace("\\", "/")] = (h.hexdigest(), info.file_size)
    return out


def _sign_key_b64(with_src=False):
    """发布私钥（base64）。按顺序找多个位置，别只认一个目录。

    v1.9.11：原来三个签名点都只认 `<上级>\_v190_work\keys\release_ed25519.key`，
    而清理临时目录时那个目录会被删掉 —— 于是打包「成功」，但三份清单全部未签名，
    最后是增量包被自己的 ps1 拒绝。现在兜底到 `_keys\`（备份位）与 `keys\`。
    """
    import base64
    env = (os.environ.get("XJY_SIGN_KEY") or "").strip()
    if env:
        return (env, "环境变量 XJY_SIGN_KEY") if with_src else env
    parent = os.path.dirname(os.path.abspath(ROOT))
    cands = [
        os.path.join(parent, "_v190_work", "keys", "release_ed25519.key"),
        os.path.join(parent, "_keys", "release_ed25519.key"),
        os.path.join(parent, "keys", "release_ed25519.key"),
        os.path.join(os.path.abspath(ROOT), "keys", "release_ed25519.key"),
    ]
    for p in cands:
        if os.path.isfile(p):
            try:
                raw = open(p, "rb").read()
            except Exception:
                continue
            if len(raw) != 32:
                log("    ！私钥文件长度不对（%d 字节，应为 32）：%s" % (len(raw), p))
                continue
            kb = base64.b64encode(raw).decode("ascii")
            return (kb, p) if with_src else kb
    if with_src:
        log("    ！没找到发布私钥：清单将不签名（增量包会被 ps1 拒绝）。"
            "请把 release_ed25519.key 放到 %s" % cands[1])
    return ("", "") if with_src else ""


def _ver_parts(v):
    """把 '1.9.8-Beta' 拆成 (数字段元组, 是否是正式版, 预发布标识元组)。

    v1.9.11：以前这里是「非数字一律记 0」，于是 `1.9.8-Beta` 变成 (1, 9, 0)，
    比 `1.9.7.1` 的 (1, 9, 7, 1) 还小 —— 找增量包基线时会被直接跳过，
    导致 1.9.10 的增量包拿 1.9.9.3 当基线（用户实际装的是 1.9.8-Beta 就会断链）。
    现在按语义化版本（SemVer）的规则解析。
    """
    txt = str(v or "").strip()
    if not txt:
        return ((), 0, ())
    main, _, pre = txt.partition("-")
    if not pre:
        main, _, pre = txt.partition("+")
    nums = []
    for part in main.split("."):
        try:
            nums.append(int(part))
        except Exception:
            # '1.9.x' 这种脏版本：按 0 处理，但别把后面的段全吞掉
            nums.append(0)
    toks = []
    for t in pre.replace("_", ".").replace("+", ".").split("."):
        if not t:
            continue
        if t.isdigit():
            toks.append((0, int(t), ""))
        else:
            # 数字标识符 < 字母标识符（SemVer 规定），字母按小写比
            toks.append((1, 0, t.lower()))
    return (tuple(nums), 0 if toks else 1, tuple(toks))


def _ver_key(v):
    """可直接排序的版本键：数字段先比，正式版 > 预发布版。

    `1.8.9` < `1.8.10`（不是字符串比较）；`1.9.8-Beta` < `1.9.8` < `1.9.9`。
    """
    return _ver_parts(v)


def _ver_lt(a, b):
    """a 是否早于 b（同 _ver_key 语义）。"""
    return _ver_key(a) < _ver_key(b)


def step_delta_zip(version, prev_zip=""):
    """v1.8.6：出「增量更新包」—— 只装新增和改动过的文件。
    
    为什么要有它：整包 420MB，每修一个 bug 就让用户重下整包太浪费。
    
    V1.8.6 按 QA 的 P0 报告改了包结构：真正要写进程序目录的文件一律放进
    `payload/`，根目录只留清单 + apply_delta.ps1 + 应用增量包.bat + README.md。
    老结构把文件裸露在 zip 根目录，用户「解压到程序目录」就等于直接覆盖，
    脚本还没跑程序就已经被写坏了 —— 这是「解压直接覆盖」的根因。
    """
    cur = os.path.join(RELEASE_DIR, "小鲸鱼_V%s_绿色版.zip" % version)
    if not os.path.isfile(cur):
        log("    没有绿色版 zip，跳过增量包")
        return ""
    
    if not prev_zip:
        cands = []
        for fn in os.listdir(RELEASE_DIR):
            if fn.startswith("小鲸鱼_V") and fn.endswith("_绿色版.zip"):
                v = fn[len("小鲸鱼_V"):-len("_绿色版.zip")]
                if v != version:
                    cands.append((_ver_key(v), v, os.path.join(RELEASE_DIR, fn)))
        cands.sort()
        # v1.9.11：只拿「比当前版本更早」的当基线。目录里如果躺着更新版本的
        # zip（比如回滚测试留下的），不能被它选中，否则增量包会「往回退」。
        older = [c for c in cands if _ver_lt(c[1], version)]
        if older:
            prev_zip = older[-1][2]
            log("    对比上一版：%s" % os.path.basename(prev_zip))
        elif cands:
            prev_zip = cands[-1][2]
            log("    ！目录里没有比 %s 更早的绿色包，改用最旧的一份对比：%s"
                % (version, os.path.basename(prev_zip)))
    if not prev_zip or not os.path.isfile(prev_zip):
        log("    没有可对比的上一版，跳过增量包（首次发布正常）")
        return ""
    
    try:
        prev_v = os.path.basename(prev_zip)[len("小鲸鱼_V"):-len("_绿色版.zip")]
    except Exception:
        prev_v = "?"
    old = _zip_index(prev_zip)
    new = _zip_index(cur)
    
    files, removed = [], []
    for name, (h, size) in new.items():
        if name.split("/")[-1] in ("delta_manifest.json", "apply_delta.ps1",
                "应用增量包.bat"):
            continue
        old_h = old.get(name, ("", -1))[0]
        # v1.9.8-Beta：README.md 这类说明文件即使内容和上一版一样也一律带上。
        # 原因：用户可能连跳好几版（从 1.9.6 直接升到 1.9.8），而 manifest 覆盖的
        # 是当前版本的内容 —— 只要中间少装一版，README 就一直对不上哈希，表现为
        # 「程序文件被改过，为了安全不启动」，用户完全无辜。
        if old_h == h and name not in ("README.md",):
            continue
        it = {"path": name, "sha256": h, "size": size,
                "action": "replace" if name in old else "add"}
        if name in old:
            it["base_sha256"] = old_h
        files.append(it)
    for name in old:
        if name not in new:
            removed.append(name)
    files.sort(key=lambda x: x["path"])

    # v1.9.3：脚本自己的哈希也要进清单，而且必须在签名之前算。
    # 复测报告 4.6：把 ps1 里「验签」那一段整块删掉，照样能装任意 payload。
    # 现在清单记着 apply_delta.ps1 和 应用增量包.bat 的 sha256：
    # ps1 启动第一件事是自校验，后端 --verify-delta 也会再核一遍。
    _ps1_bytes = DELTA_PS1.encode("utf-8-sig")
    _bat_bytes = ("\r\n".join(DELTA_BAT) + "\r\n").encode("utf-8")

    out = os.path.join(RELEASE_DIR, "小鲸鱼_V%s_增量包.zip" % version)
    if os.path.exists(out):
        os.remove(out)

    # v1.9.1（抢救二-4）：清单必须带签名。只做 sha256 自校是没用的 —— 攻击者
    # 换掉 payload、重算清单就过了；有了签名，改任何字节都得有发布私钥才能重签。
    import base64 as _b64
    _key = _sign_key_b64()
    _priv = None
    if _key:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey)
            _priv = Ed25519PrivateKey.from_private_bytes(_b64.b64decode(_key)[:32])
        except Exception as _e:
            log("    增量包私钥读不出来：%s" % _e)
            _priv = None

    # V1.9.7.1（P0-补）：安装版布局的用户，清单在 resources\ 里，而且那份清单
    # 记的是「打包器改过 3 个字节」的主程序（见 sha256_of_installed_exe）。
    # 绿色版这份清单在这两点上都对不上 —— 只带一份的话，装过安装版的用户
    # 一更新就会自判篡改。所以包里再带一份安装版清单，由 ps1 按布局挑一份。
    _inst_text = ""
    _inst_sig = b""
    _inst_exe = b""
    _need_exe = any(f["path"] == "小鲸鱼.exe" for f in files)
    if _priv is not None:
        try:
            with zipfile.ZipFile(cur) as _z:
                _green_man = _z.read("manifest.sha256").decode("utf-8")
            _ih = sha256_of_installed_exe(built_exe_path())
            if _ih:
                # 先筛成安装形态的文件集（安装包不装 README/使用说明），再换哈希
                _inst_text = swap_manifest_hash(
                    install_manifest_text(_green_man), "小鲸鱼.exe", _ih)
                _inst_sig = _priv.sign(_inst_text.encode("utf-8"))
                if _need_exe:
                    # 这一版换了主程序：安装版要的是「打包器改过 3 个字节」的那一份，
                    # 绿色版 payload 里那份盖上去会自判篡改 —— 单独带一份。
                    with open(built_exe_path(), "rb") as _fx:
                        _ib = _fx.read()
                    if _ib.count(_TAURI_BUNDLE_UNK) == 1:
                        _ib = _ib.replace(_TAURI_BUNDLE_UNK, _TAURI_BUNDLE_NSS)
                    if hashlib.sha256(_ib).hexdigest() == _ih:
                        _inst_exe = _ib
                    else:
                        log("    ！安装形态的主程序字节对不上，安装版增量包只能靠完整安装包")
            else:
                log("    ！算不出安装形态的主程序哈希，增量包将只服务绿色版布局")
        except Exception as _e:
            log("    ！安装版清单生成失败（增量包将只服务绿色版布局）：%s" % _e)
            _inst_text, _inst_sig, _inst_exe = "", b"", b""

    man = {
        "kind": "release-delta",
        "from_version": prev_v,
        "to_version": version,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "只含新增和改动过的文件，都在 payload/ 里；解压后双击「应用增量包.bat」",
        "files": files,
        "remove": removed,
            "scripts": {
                "apply_delta.ps1": hashlib.sha256(_ps1_bytes).hexdigest(),
                "应用增量包.bat": hashlib.sha256(_bat_bytes).hexdigest(),
            },
    }
    if _inst_text:
        _it_b = _inst_text.encode("utf-8")
        _inst_files = [
            {"path": "install/manifest.sha256",
             "dest": "resources/manifest.sha256",
             "sha256": hashlib.sha256(_it_b).hexdigest(),
             "size": len(_it_b)},
            {"path": "install/manifest.sig",
             "dest": "resources/manifest.sig",
             "sha256": hashlib.sha256(_inst_sig).hexdigest(),
             "size": len(_inst_sig)},
        ]
        if _inst_exe:
            _inst_files.append(
                {"path": "install/小鲸鱼.exe",
                 "dest": "小鲸鱼.exe",
                 "sha256": hashlib.sha256(_inst_exe).hexdigest(),
                 "size": len(_inst_exe)})
        man["install_layout"] = {
            "note": "安装版布局（清单在 resources\\ 里）用这一份；ps1 会按布局自动挑",
            "drop": ["manifest.sha256", "manifest.sig"],
            "files": _inst_files,
        }
    if _priv is not None:
        try:
            _txt = json.dumps(man, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
            man["sig"] = _b64.b64encode(
                _priv.sign(_txt.encode("utf-8"))).decode("ascii")
            log("    增量包清单已签名")
        except Exception as _e:
            man["sig"] = ""
            log("    增量包签名失败：%s" % _e)
    else:
        man["sig"] = ""
        log("    没有签名私钥，增量包清单未签名（ps1 会拒绝应用）")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("delta_manifest.json",
                json.dumps(man, ensure_ascii=False, indent=2).encode("utf-8"))
        z.writestr("apply_delta.ps1", _ps1_bytes)
        z.writestr("应用增量包.bat", _bat_bytes)
        z.writestr("README.md", DELTA_README.encode("utf-8"))
        with zipfile.ZipFile(cur) as src_z:
            for f in files:
                # 注意 payload/ 前缀：这是 V1.8.6 的 P0 修复，别去掉
                z.writestr("payload/" + f["path"], src_z.read(f["path"]))
        if _inst_text:
            # V1.9.7.1：安装版布局用的那一份清单（绿色版那份在 payload/ 根下）
            z.writestr("payload/install/manifest.sha256", _inst_text.encode("utf-8"))
            z.writestr("payload/install/manifest.sig", _inst_sig)
        if _inst_exe:
            z.writestr("payload/install/小鲸鱼.exe", _inst_exe)
    
    if not files:
        log("    这一版和上一版所有文件都一样，增量包只含清单（%s）"
                % human(os.path.getsize(out)))
    else:
        _ratio = 100.0 * os.path.getsize(out) / max(1, os.path.getsize(cur))
        log("[6/6] 增量包 -> %s（%s，%d 个文件，占整包的 %.1f%%）"
                % (os.path.basename(out), human(os.path.getsize(out)), len(files), _ratio))
    return out


def step_install_selfcheck(installer):
    """v1.9.8（P0 硬门禁）：把安装包装一遍，跑 --verify-self，不是 0 就中断打包。

    为什么非要真装：NSIS 打包器会把安装包里那份主程序的
    `__TAURI_BUNDLE_TYPE_VAR_UNK` 改成 `..._NSS`（3 个字节），而
    target\\release 里那份 exe 不变。只要「签名之后安装器还会再动字节」，
    "装完就自判篡改" 就必然发生 —— V1.9.7 正是这样漏出去的，而合成目录
    永远测不出来（它不经过 bundler）。
    """
    import subprocess
    import tempfile
    import shutil

    CREATE_NO_WINDOW = 0x08000000
    log("[4.5/6] 模拟安装形态自检：真装一次 + --verify-self（不是 0 就中断打包）…")

    tmp = tempfile.mkdtemp(prefix="xjy-inst-check-")
    target = os.path.join(tmp, "app")
    data = os.path.join(tmp, "data")
    resf = os.path.join(tmp, "self.txt")
    os.makedirs(target, exist_ok=True)
    os.makedirs(data, exist_ok=True)

    rc = -1
    detail = ""
    try:
        log("    静默安装到：%s" % target)
        p = subprocess.run([installer, "/S", "/D=" + target],
                           cwd=os.path.dirname(installer),
                           creationflags=CREATE_NO_WINDOW, timeout=900)
        if p.returncode != 0:
            raise SystemExit("安装包自己装不上（退出码 %d）：%s" % (p.returncode, installer))

        exe = os.path.join(target, "小鲸鱼.exe")
        if not os.path.isfile(exe):
            raise SystemExit("装完没有主程序：%s" % exe)

        env = dict(os.environ)
        env["QQ_AGENT_DATA_DIR"] = data
        r2 = subprocess.run([exe, "--verify-self", "--result-file", resf],
                            env=env, creationflags=CREATE_NO_WINDOW, timeout=600)
        rc = r2.returncode
        if os.path.isfile(resf):
            try:
                with open(resf, "r", encoding="utf-8", errors="replace") as f:
                    lines = [x.rstrip("\r\n") for x in f.readlines()]
            except OSError:
                lines = []
            if lines and lines[0].strip().isdigit():
                rc = int(lines[0].strip())
                detail = " / ".join([x for x in lines[1:3] if x.strip()])
        if rc != 0:
            raise SystemExit(
                "装出来的程序自检不通过（rc=%s）—— 这一包发出去就是「一装就变砖」，"
                "已中断打包。%s" % (rc, detail))
        log("    安装形态自检通过：rc=0%s" % (("  " + detail) if detail else ""))
    finally:
        un = os.path.join(target, "uninstall.exe")
        try:
            if os.path.isfile(un):
                subprocess.run([un, "/S"], creationflags=CREATE_NO_WINDOW, timeout=600)
                time.sleep(3)
        except Exception as e:
            log("    （卸载没跑完：%s，稍后删目录）" % e)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-napcat", action="store_true", help="不下载/不检查 NapCat 离线包")
    ap.add_argument("--no-install", action="store_true", help="只打后端，不出安装包")
    a = ap.parse_args()

    t0 = time.time()
    step_bundle(a.skip_napcat)
    log("[2/6] 打音视频微服务 exe（av_server.exe，约 2 分钟）…")
    step_av_exe()
    log("[3/6] 打后端 exe …")
    step_backend()
    if a.no_install:
        log("--no-install：跳过安装包与绿色版")
        return
    log("[4/6] 打 Tauri 安装包（首次编译约 3~5 分钟）…")
    sync_av_module()
    step_tauri()
    installer = step_collect()
    step_install_selfcheck(installer)
    green = step_green_zip()
    tools = step_tools_zip(app_version())
    src = step_source_zip(app_version())
    delta = ""
    try:
        delta = step_delta_zip(app_version())
    except Exception as _e:
        log("    增量包生成失败（不影响其它产物）：%s" % _e)

    log("")
    log("=" * 62)
    for p in [x for x in (installer, green, tools, src, delta) if x]:
        log("%-34s %10s  sha256=%s" % (os.path.basename(p), human(os.path.getsize(p)),
                                       sha256(p)[:16] + "…"))
    log("用时 %.1f 分钟" % ((time.time() - t0) / 60.0))
    log("=" * 62)


if __name__ == "__main__":
    main()
