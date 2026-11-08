# -*- coding: utf-8 -*-
"""V1.9.7.1 安全修复自测（不用 pytest，直接 python 跑）

覆盖复测报告里被点名的四处：
  P0  signing.verify()      —— 安装包布局（清单在 resources/）不再判「被篡改」
  P1  config._guard_key()   —— 1.9.5/1.9.6 的 32 字节老防护章静默升级，不动用户的开关
  P1  apply_delta.ps1       —— 只关「这个目录里」的进程（见 tests 里的 grep 检查）
  P2  verify_bundle --dir   —— 支持安装包布局；恢复工具启动时校验验签器自身的 sha256

用法：
    python tests\\test_v1971_fix.py             # 全部
    python tests\\test_v1971_fix.py signing     # 只跑一组
    python tests\\test_v1971_fix.py guard

需要 cryptography（签名）、以及本机装了 DPAPI（Windows 自带）。
跟真实私钥/真实主程序有关的两组（bundle / recovery / rust）在没有这些文件时会自动跳过，
不会把「跳过」算成通过。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import signing  # noqa: E402

PY = sys.executable
VERIFY_EXE = os.path.join(ROOT, "Tools", "verify_bundle.exe")
RECOVERY_PS1 = os.path.join(ROOT, "Tools", "恢复官方安装.ps1")
PACK_PY = os.path.join(ROOT, "打包发布.py")
RUST_EXE = os.path.join(ROOT, "desktop", "src-tauri", "target", "release", "小鲸鱼.exe")
KEY_FILE = r"D:\临时文件\小鲸鱼_程序包\_v190_work\keys\release_ed25519.key"
WORK = os.path.join(tempfile.gettempdir(), "xjy_v1971_test")

_fails = []
_oks = 0
_skips = 0


def ck(cond, label, extra=""):
    global _oks
    if cond:
        _oks += 1
        print("   [ok]   " + label)
    else:
        _fails.append(label)
        print("   [FAIL] " + label + (("   --  " + str(extra)) if extra else ""))


def skip(label, why):
    global _skips
    _skips += 1
    print("   [skip] " + label + "   --  " + why)


def head(t):
    print("")
    print("== " + t + " " + "=" * max(0, 60 - len(t)))


def fresh(name):
    p = os.path.join(WORK, name)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    return p


def runb(cmd, cwd=None, env=None):
    """跑子进程，带回 rc 和尽量还原的文本（控制台可能是 GBK）。"""
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd or ROOT, env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout or b""
    for enc in ("utf-8", "gbk"):
        try:
            return p.returncode, out.decode(enc)
        except Exception:
            pass
    return p.returncode, out.decode("utf-8", "replace")


# ------------------------------------------------------------------ 造包工具

def new_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw,
                           serialization.PrivateFormat.Raw,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)
    return seed, base64.b64encode(pub).decode("ascii")


def release_key():
    """发布私钥：可能是 32 字节裸种子，也可能是 base64/hex 文本。"""
    if not os.path.isfile(KEY_FILE):
        return None
    try:
        raw = open(KEY_FILE, "rb").read().strip()
    except Exception:
        return None
    if len(raw) == 32:
        return raw
    txt = raw.decode("ascii", "ignore").strip()
    try:
        if re.fullmatch(r"[0-9a-fA-F]{64}", txt):
            return bytes.fromhex(txt)
        out = base64.b64decode(txt, validate=True)
        if len(out) == 32:
            return out
    except Exception:
        pass
    return None


def sign_pkg(pkg_dir, seed, into=None):
    """清单以 pkg_dir 为基准算哈希，写到 into（默认 pkg_dir）——就是打包脚本的写法。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    text = signing.build_manifest(pkg_dir, signing.VERIFY_PATTERNS)
    dest = into or pkg_dir
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, signing.MANIFEST), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(text.encode("utf-8"))
    with open(os.path.join(dest, signing.SIG), "wb") as f:
        f.write(sig)
    return text


def make_pkg(base, layout, seed, exe_src=None):
    """layout='green'（清单在包根）或 'install'（清单在 resources/，跟安装包一样）。"""
    res = os.path.join(base, "resources")
    os.makedirs(res, exist_ok=True)
    exe = os.path.join(base, "小鲸鱼.exe")
    if exe_src:
        shutil.copy2(exe_src, exe)
    else:
        with open(exe, "wb") as f:
            f.write(b"MZ fake shell")
    with open(os.path.join(res, "backend_server.exe"), "wb") as f:
        f.write(b"MZ fake backend")
    with open(os.path.join(res, "napcat_bundle.zip"), "wb") as f:
        f.write(b"PK fake bundle")
    if layout == "install":
        sign_pkg(base, seed, into=res)
    else:
        sign_pkg(base, seed)
    return base


# ------------------------------------------------------------------ 1) signing

def t_signing():
    head("1) signing.py：四种形态都要能过（P0）")
    seed, pub = new_key()
    old_pub = signing.PUBLIC_KEY_B64
    try:
        for layout in ("green", "install"):
            base = make_pkg(fresh("pkg_" + layout), layout, seed)
            st = signing.verify(base, public_key_b64=pub, release=True)
            ck(bool(st.get("ok")) and not st.get("tampered"),
               "[%s 布局] verify(release=True) 通过" % layout, st.get("msg"))
            ck(bool(st.get("signed")), "[%s 布局] 认得出「有签名」" % layout, st)

            root = signing.find_package_root(base)
            ck(root and os.path.normcase(os.path.abspath(root)) ==
               os.path.normcase(os.path.abspath(base)),
               "[%s 布局] find_package_root 返回安装目录" % layout, root)

            # 被改过的文件必须抓出来（修复不能把校验一起放水）
            tgt = os.path.join(base, "resources", "backend_server.exe")
            with open(tgt, "wb") as f:
                f.write(b"MZ tampered backend")
            st2 = signing.verify(base, public_key_b64=pub, release=True)
            ck(bool(st2.get("tampered")), "[%s 布局] 改过的文件被抓出来" % layout, st2.get("msg"))
            make_pkg(base, layout, seed)      # 还原

            # 直接把 resources 目录当参数传进去（增量包/手工检查会这么用）
            res = os.path.join(base, "resources")
            st3 = signing.verify(res, public_key_b64=pub, release=True)
            ck(bool(st3.get("ok")) and not st3.get("tampered"),
               "[%s 布局] 传 resources 目录也能过" % layout, st3.get("msg"))

        # 空目录：release 下仍然要报「找不到签名清单」，不能被兜底放过
        empty = fresh("pkg_empty")
        with open(os.path.join(empty, "小鲸鱼.exe"), "wb") as f:
            f.write(b"MZ fake shell")
        st4 = signing.verify(empty, public_key_b64=pub, release=True)
        ck(bool(st4.get("tampered")) and "找不到签名清单" in str(st4.get("msg")),
           "[无清单] 仍然判被篡改", st4.get("msg"))

        # verify_program_dir：安装形态里主程序必须在清单里
        base = make_pkg(fresh("pkg_vpd"), "install", seed)
        old_self, old_rel = sys.executable, signing._is_release
        had_frozen = hasattr(sys, "frozen")
        old_frozen = getattr(sys, "frozen", None)
        try:
            signing._is_release = lambda: True
            signing.PUBLIC_KEY_B64 = pub
            # find_package_root() 在「冻结」时从 sys.executable 起步，这里照着真实
            # 运行时的两个位置各测一遍：主程序在包根、以及 Python 侧跑在
            # resources\backend_server.exe 里（安装包的真实形态）。
            sys.frozen = True
            for exe_rel, tag in (("小鲸鱼.exe", "主程序在包根"),
                                 (os.path.join("resources", "backend_server.exe"),
                                  "Python 侧在 resources 里")):
                sys.executable = os.path.join(base, exe_rel)
                v = signing.verify_program_dir(base)
                ck(bool(v.get("ok")) and not v.get("tampered"),
                   "[验证入口] %s 时通过" % tag, v.get("msg"))
            # 主程序不在清单里 -> 必须拦
            sys.executable = os.path.join(base, "别的程序.exe")
            with open(sys.executable, "wb") as f:
                f.write(b"MZ other")
            v2 = signing.verify_program_dir(base)
            ck("不在安装包的清单里" in str(v2.get("msg")), "[验证入口] 非清单内的程序被拦", v2.get("msg"))
        finally:
            sys.executable, signing._is_release = old_self, old_rel
            signing.PUBLIC_KEY_B64 = old_pub
            if had_frozen:
                sys.frozen = old_frozen
            elif hasattr(sys, "frozen"):
                del sys.frozen
    finally:
        signing.PUBLIC_KEY_B64 = old_pub


# ------------------------------------------------------------------ 2) guard

_GUARD_CHILD = r'''
import base64, hashlib, hmac, json, os, sys
sys.path.insert(0, %(root)r)
kind = sys.argv[1]
import security
import config

key = (b"xjy-v1971-test-key-" * 2)[:32]
assert len(key) == 32, len(key)
fp = config._guard_fp(key)
seal = os.path.join(os.path.dirname(config.CONFIG_PATH), "guard.seal")
if kind == "legacy32":
    blob = key
elif kind == "ok48":
    blob = key + fp
elif kind == "badlen":
    blob = key[:24]
elif kind == "raw":
    blob = None
else:
    blob = None
if kind == "raw":
    open(seal, "wb").write(b"raw:" + base64.b64encode(key))
elif kind != "none":
    enc = security._dpapi(blob, protect=True)
    open(seal, "wb").write(b"dpapi:" + base64.b64encode(enc))
cfg = dict((k, True) for k in config._GUARD_KEYS)
cfg["guard_sql"] = False
cfg["guard_prompt"] = False
body = "guard|1.9.4|" + ",".join("%%s=%%d" %% (k, 1 if cfg[k] else 0) for k in config._GUARD_KEYS)
cfg["guard_sig"] = hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False)
loaded = config.load()
raw = open(seal, "rb").read()
seal_info = {}
if raw.startswith(b"dpapi:"):
    b2 = security._dpapi(base64.b64decode(raw[6:]), protect=False) or b""
    seal_info = {"len": len(b2), "key_same": b2[:32] == key,
                 "fp_ok": len(b2) == 48 and b2[32:] == config._guard_fp(b2[:32])}
out = {"kind": kind, "tampered": config._GUARD_SEAL_TAMPERED,
       "switches": dict((k, bool(loaded.get(k))) for k in config._GUARD_KEYS),
       "seal": seal_info}
print("RESULT:" + json.dumps(out, ensure_ascii=False))
'''


def guard_run(kind):
    d = fresh("guard_" + kind)
    script = os.path.join(d, "_child.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_GUARD_CHILD % {"root": ROOT})
    rc, out = runb([PY, script, kind], cwd=ROOT,
                   env={"QQ_AGENT_DATA_DIR": d, "PYTHONIOENCODING": "utf-8"})
    res = None
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            res = json.loads(line[7:])
    return rc, res, out


def t_guard():
    head("2) config.py：老防护章静默升级，不乱动开关（P1）")
    if not os.path.isdir(os.path.join(ROOT, "security.py")) and not os.path.isfile(
            os.path.join(ROOT, "security.py")):
        skip("防护章升级", "找不到 security.py（DPAPI 封装），本机跑不了")
        return

    rc, res, out = guard_run("legacy32")
    if res is None:
        ck(False, "[老格式 32 字节] 子进程没给出结果", out[-800:])
        return
    ck(not res["tampered"], "[老格式 32 字节] 不判「被改过/被换锁」", res["tampered"])
    ck(res["switches"]["guard_sql"] is False and res["switches"]["guard_prompt"] is False,
       "[老格式 32 字节] 用户关掉的开关原样保留（不强制打开）", res["switches"])
    ck(res["seal"].get("len") == 48 and res["seal"].get("fp_ok") and res["seal"].get("key_same"),
       "[老格式 32 字节] 顺手升级成 48 字节（同一个密钥）", res["seal"])

    rc, res2, out2 = guard_run("ok48")
    if res2 is None:
        ck(False, "[本版 48 字节] 子进程没给出结果", out2[-800:])
        return
    ck(not res2["tampered"], "[本版 48 字节] 正常通过", res2["tampered"])
    ck(res2["switches"]["guard_sql"] is False,
       "[本版 48 字节] 开关照旧", res2["switches"])
    ck(res2["seal"].get("key_same"), "[本版 48 字节] 密钥没被重写", res2["seal"])

    rc, res3, out3 = guard_run("badlen")
    if res3 is None:
        ck(False, "[长度不对] 子进程没给出结果", out3[-800:])
        return
    ck(res3["tampered"] == "fp", "[长度不对] 仍然报警（fp）", res3["tampered"])
    ck(all(res3["switches"].values()), "[长度不对] 五个开关强制打开", res3["switches"])

    rc, res4, out4 = guard_run("raw")
    if res4 is None:
        ck(False, "[raw: 前缀] 子进程没给出结果", out4[-800:])
        return
    ck(res4["tampered"] == "raw", "[raw: 前缀] 仍然判「被换锁」", res4["tampered"])
    ck(all(res4["switches"].values()), "[raw: 前缀] 五个开关强制打开", res4["switches"])


# ------------------------------------------------------------------ 3) 增量包

def t_delta():
    head("3) apply_delta.ps1：按路径关进程，不按名字乱杀（P1）")
    src = open(PACK_PY, encoding="utf-8").read()
    m = re.search(r"DELTA_PS1\s*=\s*r'''(.+?)'''", src, re.S)
    if not m:
        ck(False, "打包发布.py 里找不到 DELTA_PS1")
        return
    ps = m.group(1)
    ck("function Get-TargetProcs" in ps, "有 Get-TargetProcs（按路径过滤）")
    ck("$pp.StartsWith($Target" in ps or "$pp.StartsWith($Target + " in ps,
       "按 $Target 前缀比对路径")
    bad = [ln.strip() for ln in ps.splitlines()
           if "Get-Process -Name" in ln]
    ck(not bad, "没有 Get-Process -Name 一刀切了", bad)
    ck("Stop-Process" not in ps or "Get-TargetProcs" in ps, "杀进程只经由 Get-TargetProcs")
    # 语法：交给 PowerShell 解析器（不执行）
    tmp = os.path.join(fresh("delta_parse"), "apply_delta.ps1")
    with open(tmp, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    ps_cmd = ("$e=$null;$t=$null;"
              "[void][System.Management.Automation.Language.Parser]::ParseFile("
              "'%s',[ref]$t,[ref]$e);"
              "if($e.Count -gt 0){$e|ForEach-Object{Write-Output $_.Message};exit 1}"
              "Write-Output 'PARSE-OK'" % tmp.replace("'", "''"))
    rc, out = runb(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd])
    ck(rc == 0 and "PARSE-OK" in out, "apply_delta.ps1 语法能被 PowerShell 解析", out[-600:])


# ------------------------------------------------------------------ 4) verify_bundle --dir

def t_bundle():
    head("4) verify_bundle.exe --dir：安装包布局也能认（P2）")
    if not os.path.isfile(VERIFY_EXE):
        skip("verify_bundle", "没有 Tools\\verify_bundle.exe")
        return
    seed = release_key()
    if seed is None:
        skip("verify_bundle --dir", "没有发布私钥，造不出「真签名」的测试包")
        return
    for layout in ("green", "install"):
        base = make_pkg(fresh("bundle_" + layout), layout, seed)
        rc, out = runb([VERIFY_EXE, "--dir", base])
        ck(rc == 0, "[%s 布局] --dir 返回 0" % layout, (rc, out.strip()[:300]))
        ck(out.strip().startswith("OK"), "[%s 布局] 输出以 OK 开头" % layout, out.strip()[:120])
        ck(out.isascii(), "[%s 布局] 输出仍然是纯 ASCII" % layout, out.strip()[:120])

        tgt = os.path.join(base, "resources", "backend_server.exe")
        with open(tgt, "wb") as f:
            f.write(b"MZ tampered")
        rc2, out2 = runb([VERIFY_EXE, "--dir", base])
        ck(rc2 == 4, "[%s 布局] 改过的文件返回 4" % layout, (rc2, out2.strip()[:300]))

    # zip 模式不能因为这次改动坏掉
    base = make_pkg(fresh("bundle_zip"), "green", seed)
    zpath = os.path.join(WORK, "bundle_zip.zip")
    if os.path.exists(zpath):
        os.remove(zpath)
    shutil.make_archive(zpath[:-4], "zip", base)
    rc3, out3 = runb([VERIFY_EXE, zpath])
    ck(rc3 == 0, "[zip] 官方 zip 通过", (rc3, out3.strip()[:300]))
    rc4, out4 = runb([VERIFY_EXE])
    ck(rc4 == 2, "[用法] 空参数返回 2", rc4)


# ------------------------------------------------------------------ 5) 恢复工具

def t_recovery():
    head("5) 恢复官方安装.ps1：验签器自校验 + 能吃安装包目录（P2）")
    if os.name != "nt":
        skip("恢复工具", "只能在 Windows 上跑")
        return
    seed = release_key()
    if seed is None:
        skip("恢复工具", "没有发布私钥")
        return
    if not os.path.isfile(RUST_EXE):
        skip("恢复工具", "没有现成的主程序，自检那一步跑不起来")
        return

    tools = fresh("tools")
    for n in ("恢复官方安装.ps1", "恢复官方安装.bat", "verify_bundle.exe", "使用说明.txt"):
        p = os.path.join(ROOT, "Tools", n)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(tools, n))
    ps1 = os.path.join(tools, "恢复官方安装.ps1")

    def run_ps1(target, source):
        return runb(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", ps1, "-Target", target, "-Source", source, "-Quiet"])

    # 5.1 验签器被换掉 -> 必须在动文件之前停下
    bad_tools = fresh("tools_bad")
    for n in ("恢复官方安装.ps1", "恢复官方安装.bat"):
        shutil.copy2(os.path.join(ROOT, "Tools", n), os.path.join(bad_tools, n))
    shutil.copy2(os.path.join(ROOT, "Tools", "verify_bundle.exe"),
                 os.path.join(bad_tools, "verify_bundle.exe"))
    with open(os.path.join(bad_tools, "verify_bundle.exe"), "ab") as f:
        f.write(b"\0")      # 只改一个字节：哈希就变了
    tgt = fresh("tgt_bad")
    with open(os.path.join(tgt, "小鲸鱼.exe"), "wb") as f:
        f.write(b"MZ broken")
    src = make_pkg(fresh("src_for_bad"), "green", seed, exe_src=RUST_EXE)
    rc, out = runb(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", os.path.join(bad_tools, "恢复官方安装.ps1"),
                    "-Target", tgt, "-Source", src, "-Quiet"])
    ck(rc == 2, "[验签器被换] 退出码 2", rc)
    ck("换过验签器" in out or "哈希对不上" in out, "[验签器被换] 说清了原因", out[-500:])
    ck(not os.path.isfile(os.path.join(tgt, "manifest.sha256")),
       "[验签器被换] 一个文件都没写进去")

    # 5.2 绿色版来源 -> 正常恢复
    tgt1 = fresh("tgt_green")
    with open(os.path.join(tgt1, "小鲸鱼.exe"), "wb") as f:
        f.write(b"MZ broken shell")
    with open(os.path.join(tgt1, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"nick": "老用户的数据"}, f, ensure_ascii=False)
    src1 = make_pkg(fresh("src_green"), "green", seed, exe_src=RUST_EXE)
    rc, out = run_ps1(tgt1, src1)
    ck(rc == 0, "[绿色版来源] 恢复成功退出码 0", (rc, out[-700:]))
    ck("恢复完成" in out, "[绿色版来源] 自检通过打了勾", out[-400:])
    ck(os.path.isfile(os.path.join(tgt1, "manifest.sha256")), "[绿色版来源] 根目录有清单")
    ck(json.load(open(os.path.join(tgt1, "config.json"), encoding="utf-8")).get("nick") == "老用户的数据",
       "[绿色版来源] 用户数据没动")

    # 5.3 安装包解出来的目录当来源，且目标里留着上一版的根清单（必须被清掉）
    tgt2 = fresh("tgt_install")
    with open(os.path.join(tgt2, "小鲸鱼.exe"), "wb") as f:
        f.write(b"MZ broken shell")
    with open(os.path.join(tgt2, "manifest.sha256"), "w", encoding="utf-8") as f:
        f.write("0000  stale\n")
    with open(os.path.join(tgt2, "manifest.sig"), "wb") as f:
        f.write(b"stale sig")
    src2 = make_pkg(fresh("src_install"), "install", seed, exe_src=RUST_EXE)
    rc, out = run_ps1(tgt2, src2)
    ck(rc == 0, "[安装包目录来源] 恢复成功退出码 0", (rc, out[-700:]))
    ck(os.path.isfile(os.path.join(tgt2, "resources", "manifest.sha256")),
       "[安装包目录来源] resources 下有清单")
    ck(not os.path.exists(os.path.join(tgt2, "manifest.sha256")),
       "[安装包目录来源] 过期的根清单被清掉了")
    rc_self, out_self = runb([os.path.join(tgt2, "小鲸鱼.exe"), "--verify-self"])
    ck(rc_self == 0, "[安装包目录来源] 手动再跑一次 --verify-self 也是 0", (rc_self, out_self[-300:]))


# ------------------------------------------------------------------ 6) 主程序自检

def t_rust():
    head("6) 主程序 --verify-self：两种布局都要 0")
    if not os.path.isfile(RUST_EXE):
        skip("--verify-self", "没有 desktop\\src-tauri\\target\\release\\小鲸鱼.exe")
        return
    seed = release_key()
    if seed is None:
        skip("--verify-self", "没有发布私钥")
        return
    for layout in ("green", "install"):
        base = make_pkg(fresh("self_" + layout), layout, seed, exe_src=RUST_EXE)
        rc, out = runb([os.path.join(base, "小鲸鱼.exe"), "--verify-self"])
        ck(rc == 0, "[%s 布局] --verify-self 返回 0" % layout, (rc, out[-400:]))
        # 被改一个字节必须非 0
        tgt = os.path.join(base, "resources", "backend_server.exe")
        with open(tgt, "wb") as f:
            f.write(b"MZ tampered")
        rc2, out2 = runb([os.path.join(base, "小鲸鱼.exe"), "--verify-self"])
        ck(rc2 != 0, "[%s 布局] 改过的文件自检失败" % layout, (rc2, out2[-300:]))


# ------------------------------------------------------------------ 7) 安装形态清单

def _packer():
    """把 打包发布.py 当模块导进来（只用它的两个哈希 helper）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("xjy_packer", PACK_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_sig(dest_dir, text, seed):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, signing.MANIFEST), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(text)
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(text.encode("utf-8"))
    with open(os.path.join(dest_dir, signing.SIG), "wb") as f:
        f.write(sig)


def t_install():
    head("7) 安装形态清单：得按「安装包里的字节」算哈希（P0 续）")
    pk = _packer()
    UNK = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
    NSS = b"__TAURI_BUNDLE_TYPE_VAR_NSS"
    d = fresh("inst_man")
    art = os.path.join(d, "artifact.exe")
    body = b"MZ" + b"A" * 64 + UNK + b"B" * 64
    with open(art, "wb") as f:
        f.write(body)
    ck(pk.sha256_of_installed_exe(art) == hashlib.sha256(body.replace(UNK, NSS)).hexdigest(),
       "安装形态哈希 = 把包类型标记换成 NSS 之后的哈希")
    ck(open(art, "rb").read() == body, "算这个哈希不会改构建产物本身")

    man = "aa  小鲸鱼.exe\nbb  resources/backend_server.exe\n"
    out = pk.swap_manifest_hash(man, "小鲸鱼.exe", "cc")
    ck(out == "cc  小鲸鱼.exe\nbb  resources/backend_server.exe\n",
       "只换主程序那一行的哈希，别的行原样", out)

    # 安装版清单必须先筛成「安装形态真实存在的文件」：安装包不装 README / 使用说明
    gman = ("aa  小鲸鱼.exe\n"
            "bb  resources/backend_server.exe\n"
            "cc  README.md\n"
            "dd  使用说明.txt\n"
            "ee  使用说明_EN.txt\n")
    iman = pk.install_manifest_text(gman)
    ck(iman.splitlines() == ["aa  小鲸鱼.exe", "bb  resources/backend_server.exe"],
       "安装版清单只留主程序 + resources\\ 下的条目", repr(iman))
    ck("README" not in iman and "使用说明" not in iman,
       "安装版清单不带绿色版才有的 README / 使用说明（否则装完报「文件不见了」）", repr(iman))

    seed = release_key()
    if seed is None:
        skip("安装形态清单", "没有发布私钥，造不出真签名的包")
        return

    # 绿色版：主程序就是构建产物（UNK），清单在包根
    g = os.path.join(d, "green")
    gres = os.path.join(g, "resources")
    os.makedirs(gres, exist_ok=True)
    shutil.copy2(art, os.path.join(g, "小鲸鱼.exe"))
    with open(os.path.join(gres, "backend_server.exe"), "wb") as f:
        f.write(b"MZ backend v2")
    gtext = sign_pkg(g, seed)
    r = signing.verify(g, release=True)
    ck(r["ok"] and not r["tampered"], "绿色版布局：验签通过", r["msg"])

    # 安装版：主程序已被 NSIS 改成 NSS，清单得写 NSS 那份哈希（这次修的做法）
    i = os.path.join(d, "inst")
    ires = os.path.join(i, "resources")
    os.makedirs(ires, exist_ok=True)
    with open(os.path.join(i, "小鲸鱼.exe"), "wb") as f:
        f.write(body.replace(UNK, NSS))
    with open(os.path.join(ires, "backend_server.exe"), "wb") as f:
        f.write(b"MZ backend v2")
    itext = pk.swap_manifest_hash(gtext, "小鲸鱼.exe", pk.sha256_of_installed_exe(art))
    _write_sig(ires, itext, seed)
    r2 = signing.verify(i, release=True)
    ck(r2["ok"] and not r2["tampered"], "安装版布局：按 NSS 字节写清单后验签通过", r2["msg"])
    ck(signing.find_package_root(os.path.join(i, "小鲸鱼.exe")) == i,
       "安装版布局：find_package_root 找到的就是安装目录")

    # 老做法（清单写构建产物的哈希）在安装形态下必然失败 —— 这就是本次修的雷
    old = os.path.join(d, "inst_old")
    ores = os.path.join(old, "resources")
    os.makedirs(ores, exist_ok=True)
    shutil.copy2(os.path.join(i, "小鲸鱼.exe"), os.path.join(old, "小鲸鱼.exe"))
    shutil.copy2(os.path.join(ires, "backend_server.exe"), ores)
    _write_sig(ores, gtext, seed)
    r3 = signing.verify(old, release=True)
    ck((not r3["ok"]) and r3["tampered"],
       "老做法（清单记构建产物哈希）装完就自判篡改 —— 复现被修的那个雷", r3["msg"])

    # 增量包脚本里得有按布局挑清单的分支
    src = open(PACK_PY, encoding="utf-8").read()
    m = re.search(r"DELTA_PS1\s*=\s*r'''(.+?)'''", src, re.S)
    ps = m.group(1) if m else ""
    ck("$installLayout" in ps, "增量包脚本会判断是不是安装版布局")
    ck("install_layout" in ps, "增量包脚本会读 json 里的 install_layout")
    ck('"resources"' in ps and "manifest.sha256" in ps,
       "安装版清单落到 resources\\ 里")
    ck("install_layout" in src and "payload/install/manifest.sha256" in src,
       "打包脚本会往增量包里塞安装版那份清单")
    # backend 验增量包时也得把安装版那份清单一起验
    bsrc = open(os.path.join(ROOT, "backend_server.py"), encoding="utf-8").read()
    ck("install_layout" in bsrc, "backend 验增量包时也校验安装版清单")


# ------------------------------------------------------------------ 8) V1.9.8 三项新增

def _app_ui():
    """导 app_ui（它自己会 import 一堆模块，但不启动界面）。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import importlib
    return importlib.import_module("app_ui")


def t_v198():
    head("8) V1.9.8：运行期篡改、临时目录泄漏、安装形态硬门禁")

    # --- 8.1 运行期篡改：包被动过就必须重新验，不能拿进程启动时的结论糊过去
    seed, _pub = new_key()
    base = make_pkg(fresh("cache"), "green", seed, exe_src=RUST_EXE)
    old_root = signing.find_package_root
    flag = {"tampered": False}
    calls = {"n": 0}

    def fake_status(*a, **k):
        calls["n"] += 1
        return {"ok": True, "signed": True, "tampered": flag["tampered"],
                "changed": [], "missing": [], "msg": "fake", "algo": "ed25519"}

    old_status = signing.status
    signing.find_package_root = lambda *a, **k: base
    signing.status = fake_status
    try:
        au = _app_ui()
        au._SIG_CACHE = {"key": None, "r": None}
        r1 = au.verify_package(True)
        ck(bool(r1) and not r1["tampered"], "干净包第一次校验通过")
        n1 = calls["n"]
        au.verify_package()          # 没被动过
        ck(calls["n"] == n1, "没被动过就直接用指纹缓存（不重复算哈希）")
        # 有人把后端换了（大小和 mtime 都变）
        tgt = os.path.join(base, "resources", "backend_server.exe")
        with open(tgt, "wb") as f:
            f.write(b"MZ tampered at runtime, definitely different length")
        flag["tampered"] = True
        r2 = au.verify_package()     # 注意：不传 force
        ck(calls["n"] > n1, "文件一被动过就重新验（不是拿启动时的缓存糊弄）")
        ck(bool(r2) and r2["tampered"], "不传 force 也能抓到运行期篡改 ← 被修的雷", r2.get("msg"))
    finally:
        signing.find_package_root = old_root
        signing.status = old_status

    # --- 8.2 恢复工具的临时目录：函数齐、退出路径都挂上了
    ps = open(os.path.join(ROOT, "Tools", "恢复官方安装.ps1"),
              encoding="utf-8-sig").read()
    ck("function Invoke-TempCleanup" in ps and "function Register-TempCleanup" in ps,
       "恢复工具有了临时目录清理函数")
    ck(ps.count("Register-TempCleanup $") >= 3,
       "解包目录、自检结果、自检数据目录都登记了要清", ps.count("Register-TempCleanup $"))
    ck("function Clear-StaleTemp" in ps and "Clear-StaleTemp }" in ps,
       "启动时扫掉以前几次留下的残渣")
    m = re.search(r"function Fail\(.*?\n\}", ps, re.S)
    ck(bool(m) and "Invoke-TempCleanup" in m.group(0),
       "失败退出（Fail）前也清 —— 失败才是最常走的那条路")
    ck(ps.count("try { Invoke-TempCleanup }") >= 3, "每条成功退出路径也都清",
       ps.count("try { Invoke-TempCleanup }"))
    ck("$oldDataDir" in ps and "Env:\\QQ_AGENT_DATA_DIR" in ps,
       "自检用完把 QQ_AGENT_DATA_DIR 还原，别把用户数据写进临时目录")
    errs = None
    try:
        import subprocess as _sp
        r = _sp.run(["powershell", "-NoProfile", "-Command",
                     "$e=$null;$null=[System.Management.Automation.Language.Parser]::ParseFile("
                     "'" + os.path.join(ROOT, "Tools", "恢复官方安装.ps1").replace("'", "''") +
                     "',[ref]$null,[ref]$e);if($e.Count -gt 0){$e[0].Message;exit 1}else{'PARSE-OK'}"],
                    capture_output=True, text=True, timeout=120)
        errs = (r.returncode, (r.stdout or "").strip()[-200:])
    except Exception as e:
        errs = (-1, str(e))
    ck(errs[0] == 0 and "PARSE-OK" in errs[1], "改完的 ps1 语法仍然合法", errs)

    # --- 8.3 打包脚本：真装一遍 + --verify-self，不是 0 就中断
    src = open(PACK_PY, encoding="utf-8").read()
    ck("def step_install_selfcheck" in src, "打包脚本里有安装形态硬门禁")
    i1 = src.find("step_install_selfcheck(installer)")
    i2 = src.find("green = step_green_zip()")
    ck(i1 > 0 and i2 > 0 and i1 < i2, "门禁跑在绿色版/增量包之前（坏包不许继续往下打）")
    ck('"/D=" + target' in src, "门禁是真的静默安装（不是拿合成目录糊弄）")
    ck("装出来的程序自检不通过" in src, "自检不是 0 就直接中断打包")
    ck("uninstall.exe" in src and "shutil.rmtree(tmp" in src, "门禁自己装的也得自己卸干净")

    # --- 8.4 前端：每轮刷新看篡改标记、进设置页强制重验、有清理按钮
    html = open(os.path.join(ROOT, "desktop", "src", "index.html"),
                encoding="utf-8").read()
    ck("st.package && st.package.tampered" in html, "前端每轮刷新都看篡改标记（4 秒一次）")
    ck("_sigRecheckSettings" in html and "call('verify_package', true)" in html,
       "切到设置页会强制重验一次")
    ck("btnTmpClean" in html and "clean_repair_temp" in html,
       "设置页有「清理临时文件」入口")

    # --- 8.5 清理接口真的能删（用自己造的一个残渣目录验）
    au2 = _app_ui()
    import tempfile as _tf
    td = _tf.mkdtemp(prefix="xjy-repair-")
    with open(os.path.join(td, "junk.bin"), "wb") as f:
        f.write(b"x" * 4096)
    st = au2.repair_temp_status()
    ck(any(n.startswith("xjy-repair-") for n in st.get("names", [])),
       "设置页能看见临时残渣")
    rr = au2.clean_repair_temp()
    ck(not os.path.exists(td), "清理接口真的把它删了", rr.get("msg"))


# ------------------------------------------------------------------ 入口

GROUPS = {
    "signing": t_signing,
    "guard": t_guard,
    "delta": t_delta,
    "bundle": t_bundle,
    "recovery": t_recovery,
    "rust": t_rust,
    "install": t_install,
    "v198": t_v198,
}


def main():
    os.makedirs(WORK, exist_ok=True)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and args[0] == "__guard_child":
        return
    names = args or list(GROUPS)
    for n in names:
        if n not in GROUPS:
            print("不认识的组：" + n + "（可选：" + "、".join(GROUPS) + "）")
            return 2
        try:
            GROUPS[n]()
        except Exception:
            _fails.append(n + "（抛异常）")
            print("   [FAIL] " + n + " 抛异常：")
            traceback.print_exc()
    print("")
    print("-" * 64)
    print("通过 %d 项，失败 %d 项，跳过 %d 项" % (_oks, len(_fails), _skips))
    if _fails:
        for f in _fails:
            print("  × " + f)
        return 1
    print("V1.9.7.1 安全修复自测：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
