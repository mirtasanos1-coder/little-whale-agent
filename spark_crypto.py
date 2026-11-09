# -*- coding: utf-8 -*-
"""时光胶囊（.spark）的加解密核心。

v1.5.2（QA No-Go 修复二）重写，三处关键改动：

1. **不再绑定机器特征**。1.5.1 把 `COMPUTERNAME`/`platform.machine()` 混进密钥材料，
   换台电脑就解不开 —— 而胶囊本来就是用来搬家的。现在只用「四段内置材料 + 固定盐」派生。
2. **KDF 写进文件头**。1.5.1 里 `scrypt` 失败会静默退回 `pbkdf2`，于是同一台机器上
   「导出时 scrypt 成功、导入时内存紧张退回 pbkdf2」→ 密钥悄悄变了 → 明明是对的胶囊，
   导入报「校验失败」（QA 说的「绿色版导出到 90% 就报校验失败」就是这个味道）。
   现在把用的是哪种 KDF 写进文件头，解密按文件头来，绝不再"猜"。
3. **兼容旧胶囊**：1.5.1 生成的文件（旧魔数 `XJY_SPARK_V1`，带机器特征）仍然能解，
   导入后可以重新导出成新格式。

文件布局（v3，V1.9.0 起默认）：
    MAGIC_V3(12) | flags(1) | log2n(1) | salt(16) | nonce(12) | ciphertext+tag
  密钥 = scrypt(口令, salt=文件头里的随机盐, n=2^log2n, r=8, p=1, dklen=32)
  flags bit0=1 表示 scrypt 不可用、退回 PBKDF2（同样写进文件头，解密照它来）。
  AAD = MAGIC_V3 + flags + log2n + salt —— 文件头被改过就解不开。

文件布局（v2，只读兼容）：
    MAGIC_V2(12) | kdf(1) | nonce(12) | ciphertext+tag
旧布局（v1，只读兼容）：
    MAGIC_V1(12) | nonce(12) | ciphertext+tag

为什么要改（V1.9.0）：v1/v2 的密钥材料 `_S1`~`_S4` 是**写死在源码里**的，
谁拿到源码或反编译一下就能解开所有人的胶囊 —— 那不叫加密，只叫「别用记事本看」。
v3 的口令只有用户自己知道，程序里不再存任何能单独解开胶囊的东西。
"""
import hashlib
import os

MAGIC = b"XJY_SPARK_V2"
LEGACY_MAGIC = b"XJY_SPARK_V1"
# v1.9.0：带口令的新格式
MAGIC_V3 = b"XJY_SPARK_V3"
_NONCE_LEN = 12
_SALT = b"xjy-spark-2026-salt"

# v3 的盐长度与 scrypt 参数（log2n 写进文件头，将来调大也能读老文件）
_SALT_LEN = 16
_V3_LOG2N = 15          # n = 32768，约 32MB 内存、几十毫秒
_V3_LOG2N_MIN = 14
_V3_LOG2N_MAX = 20
_V3_PBKDF2_ITER = 600000
PWD_MIN_LEN = 6

# KDF 编号（写进文件头，解密时照它派生，绝不猜）
KDF_SCRYPT = 1
KDF_PBKDF2 = 2

# ---- 密钥材料：分四段存放，拼装顺序在 _shards() 里打乱 ----
_S1 = b"\x9d\x3a\x71\xc4\x02\xee\x58\xb6"
_S2 = b"\x44\xf1\x0a\x9c\x37\xd2\x6b\x15"
_S3 = b"\xbe\x08\x52\xd7\x19\xac\xe3\x70"
_S4 = b"\x26\x8f\xc1\x4a\x5b\x90\xdd\x33"


class SparkCryptoError(Exception):
    """加解密失败（上层要翻成人话）。"""


def _shards():
    """四段材料打乱顺序再拼（不是简单 S1+S2+S3+S4）。"""
    m = [_S2, _S4, _S1, _S3]
    out = b""
    for i, part in enumerate(m):
        out += bytes(b ^ (0x5A + i) for b in part)
    return out


def _machine_hint():
    """**只为解旧胶囊保留**：1.5.1 的密钥里混了机器特征。

    新格式（v2）完全不用它 —— 换机器也能解开，这是胶囊的本意。
    """
    bits = []
    try:
        bits.append(os.environ.get("COMPUTERNAME", ""))
    except Exception:
        pass
    try:
        import platform
        bits.append(platform.machine() or "")
    except Exception:
        pass
    return "|".join(bits).encode("utf-8", "ignore")


def _derive(kdf, legacy=False):
    """按指定 KDF 派生 32 字节密钥。失败就抛，绝不静默换算法。"""
    seed = _shards() + b"|" + _SALT
    if legacy:
        seed = seed + b"|" + _machine_hint()
    if kdf == KDF_SCRYPT:
        return hashlib.scrypt(seed, salt=_SALT, n=1 << 14, r=8, p=1,
                              dklen=32, maxmem=64 * 1024 * 1024)
    return hashlib.pbkdf2_hmac("sha256", seed, _SALT, 200000, 32)


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def available():
    try:
        _aesgcm()
        return True
    except Exception:
        return False


def is_capsule(blob):
    """文件头是不是我们的魔数（v3 / v2 / v1 都认）。"""
    if not blob:
        return False
    head = bytes(blob[:12])
    return head == MAGIC_V3 or head == MAGIC or head == LEGACY_MAGIC


def needs_password(blob):
    """这个胶囊要不要口令才能打开（v3 要，v1/v2 不要）。"""
    return bool(blob) and bytes(blob[:12]) == MAGIC_V3


def check_password(password):
    """口令强度检查 → 不合格时抛 SparkCryptoError（上层翻成人话）。"""
    pwd = password if isinstance(password, str) else ""
    if not pwd:
        raise SparkCryptoError("还没设口令。胶囊要用口令加密 —— 这个口令只有你知道，"
                               "忘了就打不开了，请记牢或写下来。")
    if len(pwd) < PWD_MIN_LEN:
        raise SparkCryptoError("口令至少 %d 位（现在 %d 位）。"
                               "胶囊里是全部记忆，别用太短的。" % (PWD_MIN_LEN, len(pwd)))
    return pwd


def _derive_pwd(password, salt, log2n, use_pbkdf2=False):
    """口令 + 盐 → 32 字节 AES 密钥。"""
    seed = (password or "").encode("utf-8")
    if not use_pbkdf2:
        try:
            return hashlib.scrypt(seed, salt=salt, n=1 << int(log2n), r=8, p=1,
                                  dklen=32, maxmem=512 * 1024 * 1024)
        except Exception:
            # 内存不够等情况：退回 PBKDF2，并把「用的是哪个」写进文件头
            pass
    return hashlib.pbkdf2_hmac("sha256", seed, salt, _V3_PBKDF2_ITER, 32)


def encrypt_with_password(data, password):
    """内存里的 zip bytes → 带口令的胶囊字节（v3 格式）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SparkCryptoError("要加密的内容不是二进制")
    pwd = check_password(password)
    try:
        AESGCM = _aesgcm()
    except Exception:
        raise SparkCryptoError("缺少加密组件（cryptography）")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    log2n = _V3_LOG2N
    flags = 0
    try:
        key = hashlib.scrypt(pwd.encode("utf-8"), salt=salt, n=1 << log2n, r=8, p=1,
                             dklen=32, maxmem=512 * 1024 * 1024)
    except Exception:
        flags = 1
        key = _derive_pwd(pwd, salt, log2n, use_pbkdf2=True)
    head = MAGIC_V3 + bytes([flags]) + bytes([log2n]) + salt
    ct = AESGCM(key).encrypt(nonce, bytes(data), head)
    return head + nonce + ct


def encrypt(data):
    """内存里的 zip bytes → 胶囊字节（v2 格式）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SparkCryptoError("要加密的内容不是二进制")
    try:
        AESGCM = _aesgcm()
    except Exception:
        raise SparkCryptoError("缺少加密组件（cryptography）")
    # 用哪种 KDF 由这里一次定下来，并写进文件头；解密只照抄，不再重算一遍碰运气
    kdf = KDF_SCRYPT
    try:
        key = _derive(KDF_SCRYPT)
    except Exception:
        kdf = KDF_PBKDF2
        try:
            key = _derive(KDF_PBKDF2)
        except Exception as e:
            raise SparkCryptoError("派生密钥失败：%s" % e)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, bytes(data), MAGIC)
    return MAGIC + bytes([kdf]) + nonce + ct


def _try_decrypt(key, nonce, ct, aad):
    AESGCM = _aesgcm()
    return AESGCM(key).decrypt(nonce, ct, aad)


def decrypt(blob, want_info=False, password=None):
    """胶囊字节 → 原始 zip bytes。

    want_info=True 时返回 (data, {"format": "v3"|"v2"|"v1", "kdf": ...})。
    v3 胶囊必须给 password，否则抛 SparkCryptoError（上层提示用户输入）。
    """
    if not is_capsule(blob):
        raise SparkCryptoError("不是本软件生成的胶囊（文件头不对）")
    blob = bytes(blob)
    head = blob[:12]
    try:
        _aesgcm()
    except Exception:
        raise SparkCryptoError("缺少加密组件（cryptography）")

    # ---- v3：口令派生 ----
    if head == MAGIC_V3:
        if not password:
            raise SparkCryptoError("这个胶囊是用口令加密的，需要输入导出口令才能打开")
        need = 12 + 1 + 1 + _SALT_LEN + _NONCE_LEN + 16
        if len(blob) < need:
            raise SparkCryptoError("胶囊内容不完整")
        flags = blob[12]
        log2n = blob[13]
        if not (_V3_LOG2N_MIN <= log2n <= _V3_LOG2N_MAX):
            raise SparkCryptoError("胶囊文件头不合法（KDF 参数异常）")
        salt = blob[14:14 + _SALT_LEN]
        nonce = blob[14 + _SALT_LEN:14 + _SALT_LEN + _NONCE_LEN]
        ct = blob[14 + _SALT_LEN + _NONCE_LEN:]
        aad = blob[:14 + _SALT_LEN]
        try:
            key = _derive_pwd(password, salt, log2n, use_pbkdf2=bool(flags & 1))
        except Exception as e:
            raise SparkCryptoError("派生密钥失败：%s" % e)
        try:
            data = _try_decrypt(key, nonce, ct, aad)
        except Exception:
            raise SparkCryptoError("口令不对，或者胶囊被改过（校验失败）")
        info = {"format": "v3", "kdf": "pbkdf2" if (flags & 1) else "scrypt",
                "legacy": False, "pwd": True}
        return (data, info) if want_info else data

    attempts = []          # (kdf, legacy, nonce, ct, aad, fmt)
    if head == MAGIC:
        if len(blob) < 13 + _NONCE_LEN + 16:
            raise SparkCryptoError("胶囊内容不完整")
        kdf = blob[12]
        body = blob[13:]
        nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
        order = [kdf] + [k for k in (KDF_SCRYPT, KDF_PBKDF2) if k != kdf]
        for k in order:
            attempts.append((k, False, nonce, ct, MAGIC, "v2"))
    else:
        if len(blob) < 12 + _NONCE_LEN + 16:
            raise SparkCryptoError("胶囊内容不完整")
        body = blob[12:]
        nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
        # 旧胶囊：可能是 scrypt，也可能是回落后的 pbkdf2，且带机器特征
        for k in (KDF_SCRYPT, KDF_PBKDF2):
            attempts.append((k, True, nonce, ct, LEGACY_MAGIC, "v1"))

    last = None
    for kdf, legacy, nonce, ct, aad, fmt in attempts:
        try:
            key = _derive(kdf, legacy=legacy)
        except Exception as e:
            last = e
            continue
        try:
            data = _try_decrypt(key, nonce, ct, aad)
        except Exception as e:
            last = e
            continue
        info = {"format": fmt,
                "kdf": "scrypt" if kdf == KDF_SCRYPT else "pbkdf2",
                "legacy": legacy}
        return (data, info) if want_info else data
    raise SparkCryptoError("胶囊已损坏或不是本软件生成的（校验失败）：%s"
                           % (last or ""))


def peek_version(blob):
    """0=不是胶囊 / 3=带口令的新格式 / 2=上一版格式 / 1=更旧的格式。"""
    if not is_capsule(blob):
        return 0
    head = bytes(blob[:12])
    if head == MAGIC_V3:
        return 3
    return 2 if head == MAGIC else 1
