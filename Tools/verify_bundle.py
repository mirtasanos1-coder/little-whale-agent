# -*- coding: utf-8 -*-
"""小鲸鱼 · 安装包验签器（给「恢复官方安装」用）

用法：
    verify_bundle.exe <绿色版zip>
    verify_bundle.exe --dir <已经解开的目录>
退出码：0 = 签名与内容都对得上；2 = 用法不对；4 = 验不过（不是官方包，或被改过）

为什么要有这个：恢复脚本以前直接从 zip 里取文件覆盖程序目录，全程不验来源 ——
攻击者往下载目录里放一个「版本号更大」的假绿色包，脚本就会自动选中它，
把木马写进程序目录，还打一个「✓ 官方原版」。这里用编译期写死的公钥做唯一判据：
  1) 必须有 manifest.sha256 与 manifest.sig；
  2) 用内置 Ed25519 公钥验签名；
  3) 逐条核对清单里的 sha256 与实际内容；
  4) 清单里必须真的有「小鲸鱼.exe」（只有覆盖主程序的清单才值得信）。

注意：输出一律用 ASCII。这个工具要被 Windows PowerShell 5.1 抓 stdout，
管道两端编码对不上会把中文糊成乱码（实际踩过），所以结论用英文，中文由脚本说。
"""
import base64
import hashlib
import os
import sys
import zipfile

# 与 signing.PUBLIC_KEY_B64 是同一把（发布方公钥；泄露无危害）
PUBKEY_B64 = "7hiOuUQKOs8OJaSHP/rwkDgJFzYIEVABPB8dfcIe3O4="
MANIFEST = "manifest.sha256"
SIG = "manifest.sig"
EXE = "小鲸鱼.exe"


def _say(msg):
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _parse(text):
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0].strip()) != 64:
            return None
        out.append((parts[0].strip().lower(), parts[1].strip()))
    return out


def _check(read, has, tag):
    """read(rel)->bytes；has(rel)->bool。返回 (rc, ascii_msg)。"""
    if not has(MANIFEST):
        return 4, "%s has no manifest.sha256 -- not an official bundle" % tag
    if not has(SIG):
        return 4, "%s has no manifest.sig -- unsigned, refusing" % tag
    try:
        text = read(MANIFEST).decode("utf-8", "replace")
        sig = read(SIG)
    except Exception as e:
        return 4, "%s: cannot read manifest/sig (%s)" % (tag, e)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBKEY_B64))
        pk.verify(sig, text.encode("utf-8"))
    except Exception:
        return 4, "BAD SIGNATURE -- not published by us (or manifest was edited)"
    rows = _parse(text)
    if not rows:
        return 4, "manifest format is wrong"
    need_exe = False
    ed25519_sig_ok = True
    bad = []
    for want, rel in rows:
        key = rel.replace("\\", "/")
        if key.lower() == EXE.lower():
            need_exe = True
        if not has(key):
            bad.append("%s (missing)" % rel)
            continue
        try:
            data = read(key)
        except Exception as e:
            bad.append("%s (unreadable: %s)" % (rel, e))
            continue
        if hashlib.sha256(data).hexdigest() != want:
            bad.append("%s (content changed)" % rel)
    if not ed25519_sig_ok or not need_exe:
        return 4, "manifest does not cover the main exe -- refusing"
    if bad:
        return 4, "%d file(s) mismatch: %s" % (len(bad), "; ".join(bad[:5]))
    return 0, "signature OK (%d files)" % len(rows)


def verify_zip(zip_path):
    try:
        z = zipfile.ZipFile(zip_path)
    except Exception as e:
        return 4, "cannot open zip: %s" % e
    with z:
        low = {}
        for n in z.namelist():
            low[n.replace("\\", "/").lower()] = n

        def has(rel):
            return rel.replace("\\", "/").lower() in low

        def read(rel):
            return z.read(low[rel.replace("\\", "/").lower()])

        return _check(read, has, "bundle")


def verify_dir(dir_path):
    root = os.path.abspath(dir_path)
    if not os.path.isdir(root):
        return 4, "no such directory: %s" % dir_path
    # v1.9.7.1（P2）：安装包（NSIS）把清单装在 <目录>\resources\ 里，清单里的
    # 路径仍以这个目录为基准写（resources/xxx、小鲸鱼.exe）。以前只在目录本层
    # 找清单，于是「安装包解出来的目录」永远报 no manifest.sha256（复测报告 4.2），
    # 而恢复脚本的注释一直声称它支持这种目录。
    # 现在清单/签名可以来自 <目录>\resources\，文件仍按 <目录> 解析。
    man_dirs = [root]
    sub = os.path.join(root, "resources")
    if os.path.isdir(sub):
        man_dirs.append(sub)
    md = root
    for cand in man_dirs:
        if os.path.isfile(os.path.join(cand, MANIFEST)):
            md = cand
            break
    else:
        for cand in man_dirs:
            if os.path.isfile(os.path.join(cand, SIG)):
                md = cand
                break

    def _local(rel):
        key = rel.replace("\\", "/").lower()
        if key in (MANIFEST, SIG):
            return os.path.join(md, rel.replace("/", os.sep))
        return os.path.join(root, rel.replace("/", os.sep))

    def has(rel):
        return os.path.isfile(_local(rel))

    def read(rel):
        with open(_local(rel), "rb") as f:
            return f.read()

    return _check(read, has, "directory")


def main():
    argv = [a for a in sys.argv[1:] if a]
    if not argv:
        _say("usage: verify_bundle.exe <bundle.zip>  |  verify_bundle.exe --dir <dir>")
        return 2
    if argv[0] in ("--dir", "-d"):
        if len(argv) < 2:
            _say("usage: verify_bundle.exe --dir <dir>")
            return 2
        rc, msg = verify_dir(argv[1])
    else:
        rc, msg = verify_zip(argv[0])
    _say(("OK  " if rc == 0 else "BAD ") + msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
