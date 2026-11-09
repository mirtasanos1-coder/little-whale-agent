# -*- coding: utf-8 -*-
"""V1.9.0 模块四：把四个安全相关的模块编成 .pyd（Cython）

目的不是「更快」，是「更难看懂」：`.py` 打开就是源码，`.pyd` 是编译产物，
反编译门槛高得多。选这四个是因为它们是安全地基 ——
  secure_core.py   加密/校验的底层
  spark_crypto.py  胶囊加解密（含口令派生）
  security.py      注入防护与权限
  audit.py         签署审计哈希链

用法：
    python build_cython.py            # 编译到 _cy_build/
    python build_cython.py --check    # 只检查已有产物能否 import

设计要点：
  · 产物统一命名成 `<模块名>.pyd`（不带 ABI 标签），这样冻结后的程序
    `import audit` 能直接找到它；
  · 编完立刻在当前解释器里 import 一遍并抽查关键符号 —— 编译「成功」但
    导入即崩是最坑的情况，必须在打包前就发现；
  · 任何一步失败都返回非 0，由调用方决定是降级（继续用 .py）还是中止。
"""
import glob
import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_cy_build")

TARGETS = ["secure_core", "spark_crypto", "security", "audit",
            "legacy_keys"]

# 编完要抽查的符号：{模块: [符号, ...]}。少一个就说明编出来的东西不对。
PROBES = {
	"legacy_keys": ["_tables"],
    "secure_core": ["__file__", "shard_state", "secret_salt_legacy"],
	"spark_crypto": ["encrypt_with_password", "decrypt", "needs_password",
	                 "peek_version", "MAGIC_V3"],
	"security": ["__file__"],
	"audit": ["_digest", "_digest_v1", "verify", "accept", "state",
	          "recent", "device_info", "DIGEST_ALGO"],
}

SETUP_PY = '''# -*- coding: utf-8 -*-
import sys
from setuptools import setup
from Cython.Build import cythonize

mods = %(mods)r
setup(
    name="xj_cy",
    ext_modules=cythonize(
        ["%%s.py" %% m for m in mods],
        language_level=3,
        compiler_directives={"language_level": 3, "binding": False,
                            "embedsignature": True},
        quiet=True,
    ),
    script_args=["build_ext", "--inplace"],
)
'''


def log(msg):
	print(msg)


def _build_secret_raw():
    """v1.9.3：取构建期 SECRET（原始 32 字节）。

    来源：build_backend.py 通过环境变量 XJY_BUILD_SECRET 传进来（base64）。
    这个值在本函数里只用一次 —— 用完立刻从环境里删掉，免得后面的
    PyInstaller 子进程也继承到（那等于把种子发给另一个进程）。
    """
    v = (os.environ.get("XJY_BUILD_SECRET") or "").strip()
    os.environ.pop("XJY_BUILD_SECRET", None)
    if v:
        try:
            import base64 as _b64
            raw = _b64.b64decode(v)
            if len(raw) == 32:
                return raw
        except Exception:
            pass
    import secrets
    return secrets.token_bytes(32)


def _derive(secret):
    """构建期直接算出最终的盐与清单密钥（种子不进入产物）。"""
    import hashlib as _hl
    import hmac as _hmac
    import importlib.util as _iu

    src = os.path.join(HERE, "secure_core.py")
    spec = _iu.spec_from_file_location("_sc_for_build", src)
    sc = _iu.module_from_spec(spec)
    spec.loader.exec_module(sc)
    salt = _hmac.new(sc._base_salt(), secret, _hl.sha256).digest()
    mkey = _hmac.new(sc._base_manifest_key(), secret, _hl.sha256).digest()
    return salt, mkey


def _srcmod():
    """把源码树里的 secure_core 当普通模块载入（只为了拿常量层）。"""
    import importlib.util as _iu
    src = os.path.join(HERE, "secure_core.py")
    spec = _iu.spec_from_file_location("_sc_for_build", src)
    sc = _iu.module_from_spec(spec)
    spec.loader.exec_module(sc)
    return sc


def _pack(blob):
    """按运行时的掩码公式把字节打散 —— 掩码本身不进产物（V1.9.4）。"""
    sc = _srcmod()
    return bytes((c ^ sc._mask_of(i // 4)) & 0xFF for i, c in enumerate(blob))


def _fmt_shards(packed):
    return 'b"%s"' % "".join("\\x%02x" % c for c in packed)

def _patch_secure_core(src_path, dst_path=None):
    """把"算好的盐/清单密钥"以分片形式拼进 secure_core 的编译输入。

    v1.9.2 的做法是把 SECRET 整串 base64 编进 .pyd —— 报告实测一条正则
    0.5 秒就扫出来了。v1.9.3 改成：打包机直接算出最终的盐与清单密钥，
    再把它们切成 8 片、每片异或一个掩码写进去。产物里既没有完整的密钥，
    也没有派生它的种子；就算捡到某一片，也只是 4 个无意义的字节。
    """
    s = io.open(src_path, encoding="utf-8").read()
    secret = _build_secret_raw()
    salt, mkey = _derive(secret)
    s1 = _fmt_shards(_pack(salt))
    s2 = _fmt_shards(_pack(mkey))
    for old, new in (("_SHARDS_SALT = ()", "_SHARDS_SALT = " + s1),
                     ("_SHARDS_MKEY = ()", "_SHARDS_MKEY = " + s2)):
        if old not in s:
            raise RuntimeError("secure_core.py 里找不到占位：%s" % old)
        s = s.replace(old, new, 1)
    # v1.9.3：关掉 binding。binding=True 时 Cython 会把长字符串常量塞进
    # 模块字符串表（每 8 个字符插一个 0xFF 分隔），诱饵就被切碎了 ——
    # 我们要的是「真密钥扫不到、假密钥一眼扫得到」。binding=False 时
    # 常量以连续 C 字符串落盘。
    if "# cython: binding=False" not in s:
        _ls = s.split("\n")
        _ls.insert(1 if _ls and _ls[0].startswith("#") else 0,
                   "# cython: binding=False")
        s = "\n".join(_ls)
    d = os.path.dirname(dst_path) if dst_path else os.path.join(HERE, "_cy_src")
    os.makedirs(d, exist_ok=True)
    p = dst_path or os.path.join(d, "secure_core.py")
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    return p



def build():
	missing = [m for m in TARGETS if not os.path.isfile(os.path.join(HERE, m + ".py"))]
	if missing:
		log("缺少源文件，跳过 Cython：%s" % ", ".join(missing))
		return 1
	try:
		import Cython  # noqa: F401
	except Exception as e:
		log("没装 Cython（%s），跳过编译，继续用 .py" % e)
		return 1

	os.makedirs(OUT, exist_ok=True)
	# v1.9.2：在临时目录里编译。SECRET 只出现在 _cy_src/secure_core.py 里，
	# 收完 .pyd 立刻整个删掉 —— 源码树里从头到尾没有一个带密钥的文件。
	WORK = os.path.join(HERE, "_cy_src")
	shutil.rmtree(WORK, ignore_errors=True)
	os.makedirs(WORK, exist_ok=True)
	try:
		for m in TARGETS:
			src = os.path.join(HERE, m + ".py")
			dst = os.path.join(WORK, m + ".py")
			if m == "secure_core":
				_patch_secure_core(src, dst)
			else:
				shutil.copy2(src, dst)

		setup_py = os.path.join(WORK, "_cy_setup.py")
		with open(setup_py, "w", encoding="utf-8", newline="\n") as f:
			f.write(SETUP_PY % {"mods": TARGETS})

		env = dict(os.environ)
		env["PYTHONUTF8"] = "1"
		log("Cython 编译 %d 个模块：%s" % (len(TARGETS), ", ".join(TARGETS)))
		r = subprocess.run([sys.executable, setup_py], cwd=WORK, env=env,
		                   capture_output=True, text=True, encoding="utf-8",
		                   errors="replace")
		if r.returncode != 0:
			log("编译失败（返回 %s）" % r.returncode)
			log((r.stderr or r.stdout or "")[-3000:])
			return 1

		# 收产物：把 *.pyd 统一改名成 <模块>.pyd 放进 _cy_build
		got = []
		for m in TARGETS:
			cands = sorted(glob.glob(os.path.join(WORK, m + "*.pyd")))
			cands = [c for c in cands if os.path.basename(c) != m + ".pyd"]
			if not cands:
				log("警告：%s 没有产出 .pyd" % m)
				continue
			dst = os.path.join(OUT, m + ".pyd")
			shutil.copy2(cands[0], dst)
			got.append(dst)
			log("  %-14s -> %s（%.0f KB）" % (m, os.path.basename(dst),
			                               os.path.getsize(dst) / 1024.0))
	finally:
		# 无条件清掉临时目录（含注入过 SECRET 的那份源码）
		shutil.rmtree(WORK, ignore_errors=True)
		for pat in ("*.c", "*.pyd", "build"):
			for p in glob.glob(os.path.join(HERE, pat)):
				if os.path.isdir(p):
					shutil.rmtree(p, ignore_errors=True)
				else:
					try:
						os.remove(p)
					except OSError:
						pass
	return 0 if len(got) == len(TARGETS) else 1


def check():
	# v1.9.3：陷阱环境变量必须清掉 —— 否则 secure_core 会认为「密钥是被
	# 外部拿走的」，故意返回假盐，这里的探测就失去意义了。
	for _k in ("XJY_BUILD_SECRET", "XJY_SECRET", "XJY_INJECT_SECRET",
	           "XJY_FORCE_SALT"):
		os.environ.pop(_k, None)
	"""把 _cy_build 放到 sys.path 最前，import 一遍并抽查符号。"""
	if not os.path.isdir(OUT):
		log("_cy_build 不存在，先跑 build")
		return 1
	sys.path.insert(0, OUT)
	bad = 0
	for m in TARGETS:
		p = os.path.join(OUT, m + ".pyd")
		if not os.path.isfile(p):
			log("%-14s 缺失" % m)
			bad += 1
			continue
		try:
			mod = __import__(m)
			miss = [s for s in PROBES.get(m, []) if not hasattr(mod, s)]
			if miss:
				log("%-14s 缺符号 %s" % (m, miss))
				bad += 1
			else:
				log("%-14s OK  %s" % (m, getattr(mod, "__file__", "?")))
		except Exception as e:
			log("%-14s 导入失败：%s" % (m, e))
			bad += 1
	log("=== %s ===" % ("ALL PASS" if bad == 0 else "%d FAIL" % bad))
	return 0 if bad == 0 else 1


if __name__ == "__main__":
	if "--check" in sys.argv:
		sys.exit(check())
	rc = build()
	if rc == 0:
		rc = check()
	sys.exit(rc)
