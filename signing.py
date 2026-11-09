# -*- coding: utf-8 -*-
"""V1.9.0 模块二-2：发布包签名与启动校验（Ed25519）

为什么要有这个：
  安装包/绿色版在用户机器上是可以被随便改的 —— 有人塞一个改过的
  `backend_server.exe`，它照样以「小鲸鱼」的名字跑起来，用户看不出来。
  这里给整包做一份「文件清单 + 签名」：
    · 打包时生成 `manifest.sha256`（每行 `sha256␠␠相对路径`，按路径排序）
    · 用 Ed25519 私钥对清单原文签名，写进 `manifest.sig`
    · 主程序里**只内置公钥**，启动时验签 + 逐个比对哈希
  私钥不进仓库、不进安装包（`打包发布.py` 从项目外的密钥文件或环境变量读）。

两种失败要分开：
  · 签名不对 / 清单被改 → 这是「有人在伪造」，必须拦住（返回 tampered=True）
  · 只有清单没签名（自己从源码编译的）→ 只是「未签名」，提示一下，不拦
"""
import base64
import hashlib
import io
import json
import os

MANIFEST = "manifest.sha256"
SIG = "manifest.sig"

# 内置公钥（32 字节 Ed25519 raw public key，base64）。
# 私钥只存在于项目外的构建机密钥文件里；这里是公钥，泄露也无所谓。
PUBLIC_KEY_B64 = "7hiOuUQKOs8OJaSHP/rwkDgJFzYIEVABPB8dfcIe3O4="

# 哪些文件要校验（相对包根）。只校验「会被执行/会被加载」的东西，
# 免得用户改个 README 就被判成篡改。
VERIFY_PATTERNS = (
	"小鲸鱼.exe",
	"backend_server.exe",
	"resources/backend_server.exe",
	"resources/av_module/av_server.exe",
	"resources/napcat_bundle.zip",
	"version.py",
)


class SignError(Exception):
	"""签名/校验失败（上层翻成人话）。"""


def _ed25519():
	try:
		from cryptography.hazmat.primitives.asymmetric.ed25519 import (
			Ed25519PrivateKey, Ed25519PublicKey)
		return Ed25519PrivateKey, Ed25519PublicKey
	except Exception as e:
		raise SignError("缺少签名组件（cryptography）：%s" % e)


def sha256_file(path, chunk=1024 * 1024):
	h = hashlib.sha256()
	with open(path, "rb") as f:
		while True:
			b = f.read(chunk)
			if not b:
				break
			h.update(b)
	return h.hexdigest()


# ---------------------------------------------------------------- 生成（打包用）

def build_manifest(root, patterns=None):
	"""按相对路径排序，生成清单文本。patterns 为 None 时列出全部常规文件。"""
	root = os.path.abspath(root)
	lines = []
	skip_dirs = {"__pycache__", "node_modules", "target", "dist", "build", ".git"}
	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in skip_dirs]
		for fn in filenames:
			if fn in (MANIFEST, SIG) or fn.endswith((".pyc", ".log", ".tmp")):
				continue
			full = os.path.join(dirpath, fn)
			rel = os.path.relpath(full, root).replace("\\", "/")
			if patterns:
				if not any(rel == p or rel.endswith("/" + p) or rel.startswith(p)
				           for p in patterns):
					continue
			try:
				lines.append("%s  %s" % (sha256_file(full), rel))
			except Exception:
				continue
	lines.sort(key=lambda x: x.split("  ", 1)[-1])
	return "\n".join(lines) + "\n"


def sign_manifest(root, private_key_b64, patterns=None):
	"""生成 manifest.sha256 与 manifest.sig，返回 {ok, files, sig, manifest}。"""
	Priv, _Pub = _ed25519()
	try:
		raw = base64.b64decode(private_key_b64)
	except Exception as e:
		raise SignError("私钥不是合法 base64：%s" % e)
	if len(raw) == 32:
		key = Priv.from_private_bytes(raw)
	elif len(raw) == 64:      # 有人给的是 seed+pub 的拼接
		key = Priv.from_private_bytes(raw[:32])
	else:
		raise SignError("私钥长度不对（%d 字节，应为 32）" % len(raw))

	text = build_manifest(root, patterns)
	mp = os.path.join(root, MANIFEST)
	with io.open(mp, "w", encoding="utf-8", newline="\n") as f:
		f.write(text)
	sig = key.sign(text.encode("utf-8"))
	sp = os.path.join(root, SIG)
	with open(sp, "wb") as f:
		f.write(sig)
	return {"ok": True, "manifest": mp, "sig": sp, "files": len(text.strip().split("\n")),
	        "sig_b64": base64.b64encode(sig).decode("ascii")}


# ---------------------------------------------------------------- 校验（启动时）

def _parse_manifest(text):
	out = []
	for line in (text or "").split("\n"):
		line = line.strip()
		if not line:
			continue
		parts = line.split("  ", 1)
		if len(parts) != 2:
			raise SignError("清单格式不对：%s" % line[:60])
		out.append((parts[0].lower(), parts[1].strip()))
	return out


def _is_release():
	"""当前是不是「官方发行形态」。

	冻结版（PyInstaller 打出来的 exe）就算发行版；源码里直接 python 跑算开发版。
	开发版没有清单是正常的；发行版没有清单 = 被人删过。
	"""
	import sys
	return bool(getattr(sys, "frozen", False))


def _locate_manifest(root):
	"""找出（基准目录, 清单路径, 签名路径）。

	v1.9.7.1（P0 回归）：安装包（NSIS）把清单装在 <安装目录>\\resources\\ 里，
	而清单里的路径是以**安装目录**为基准写的（resources/xxx、小鲸鱼.exe）。
	以前这里只认 <root>\\manifest.sha256，于是「用安装包装完」第一次启动就报
	「找不到签名清单 → 被篡改」，首屏遮罩进不去（复测报告 3.4）。
	现在两级兜底：先试根目录，再试 resources\\ 子目录；哈希基准始终是安装目录。
	"""
	base = os.path.abspath(root)
	mp = os.path.join(base, MANIFEST)
	if os.path.isfile(mp):
		# 反过来：调用方直接把 resources 当根传进来时（老调用方式），
		# 基准要上提一层，否则会去找 resources/resources/xxx，全部报「不见了」。
		if os.path.basename(base).lower() == "resources":
			up = os.path.dirname(base)
			return up, mp, os.path.join(base, SIG)
		return base, mp, os.path.join(base, SIG)
	sub = os.path.join(base, "resources")
	mp2 = os.path.join(sub, MANIFEST)
	if os.path.isfile(mp2):
		return base, mp2, os.path.join(sub, SIG)
	# 传的是 resources、清单却在上一层（绿色版布局 + 老调用方式）
	up = os.path.dirname(base)
	mp3 = os.path.join(up, MANIFEST)
	if os.path.basename(base).lower() == "resources" and os.path.isfile(mp3):
		return up, mp3, os.path.join(up, SIG)
	return base, mp, os.path.join(base, SIG)


def verify(root, public_key_b64=None, release=None):
	"""校验一个安装目录。

	返回 {ok, signed, tampered, missing:[], changed:[], msg}
	  ok       —— 没有发现被改的文件
	  signed   —— 有没有签名且签名有效
	  tampered —— 有人在伪造/改清单/删清单，调用方应当拦住

	v1.9.1：以前「没有清单」直接返回 ok=True，于是**删掉 manifest.sha256 和
	manifest.sig 就能绕过整套签名**（复测报告 N-03）。现在区分两种环境：
	  · 开发环境（源码运行）—— 没清单是正常的，跳过；
	  · 发行环境（冻结版）—— 没清单就是被删过，直接判 tampered。
	"""
	root = os.path.abspath(root)
	if release is None:
		release = _is_release()
	# v1.9.7.1（P0 回归）：清单可能在 <安装目录>\\resources\\ 里（安装包布局），
	# 也可能在安装目录本身（绿色版布局）。这一步把三样东西一次定好：
	# 哈希基准目录、清单路径、签名路径 —— 基准永远是「安装目录」。
	root, mp, sp = _locate_manifest(root)
	if not os.path.isfile(mp):
		if release:
			return {"ok": False, "signed": False, "tampered": True, "missing": [],
			        "changed": [],
			        "msg": "找不到签名清单（%s）。发行版必须带清单，缺了说明被删过 —— "
			               "为安全起见不启动，请从官方渠道重新下载。" % root}
		return {"ok": True, "signed": False, "tampered": False, "missing": [],
		        "changed": [], "msg": "没有签名清单（源码运行，跳过校验）"}
	# v1.9.4（抢修二①）：fail-open 第三次复发 —— 只删 manifest.sig、不动
	# manifest.sha256，signed 就一直是 False，而下面的哈希比对照常跑完并报
	# 「文件校验通过（35 个文件）」，于是拆签名等于没校验。发行版缺 sig 只有
	# 一个解释：有人在拆签名。直接判篡改，不放行。
	if release and not os.path.isfile(sp):
		return {"ok": False, "signed": False, "tampered": True,
			"missing": [], "changed": [],
			"msg": "签名文件不见了（%s）。发行版必须带 manifest.sig，缺了说明有人在"
			"拆签名 —— 为安全起见不启动，请从官方渠道重新下载。" % sp}
	text = io.open(mp, encoding="utf-8").read()

	signed = False
	if os.path.isfile(sp):
		try:
			_P, Pub = _ed25519()
			pub = Pub.from_public_bytes(
				base64.b64decode(public_key_b64 or PUBLIC_KEY_B64))
			pub.verify(open(sp, "rb").read(), text.encode("utf-8"))
			signed = True
		except Exception as e:
			return {"ok": False, "signed": False, "tampered": True, "missing": [],
			        "changed": [],
			        "msg": "签名校验不通过：这个包被改过（%s）。为安全起见不启动，"
			               "请从官方渠道重新下载。" % type(e).__name__}

	entries = _parse_manifest(text)
	missing, changed = [], []
	for want, rel in entries:
		full = os.path.join(root, rel.replace("/", os.sep))
		if not os.path.isfile(full):
			missing.append(rel)
			continue
		try:
			got = sha256_file(full)
		except Exception:
			missing.append(rel)
			continue
		if got.lower() != want:
			changed.append(rel)
	bad = bool(missing or changed)
	msg = "文件校验通过（%d 个文件）" % len(entries) if not bad else \
		"有 %d 个文件被改过、%d 个不见了" % (len(changed), len(missing))
	# v1.9.1：文件跟（已验签的）清单对不上，本身就是篡改 —— 不能只报 ok=False
	# 而把 tampered 留成 False，否则前端只看 tampered，照样放行。
	return {"ok": not bad, "signed": signed, "tampered": bad,
	        "missing": missing[:20], "changed": changed[:20], "msg": msg}


def find_package_root(start=None, up=3):
	"""从 start 开始向上最多 up 层，找到含 manifest.sha256 的目录。

	v1.9.1：冻结版里 sys.executable 是 <安装目录>\resources\backend_server.exe，
	而清单写在**包根**。以前直接拿 exe 所在目录去校验，于是永远找不到清单 ——
	整个签名体系在真实发布形态下从未执行过（复测报告 N-01）。
	"""
	import sys
	if start is None:
		start = os.path.dirname(os.path.abspath(sys.executable)) \
			if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
	base = os.path.abspath(start)
	for _ in range(up + 1):
		if os.path.isfile(os.path.join(base, MANIFEST)):
			# v1.9.7（P0）：安装包的清单装在 <安装目录>\resources\ 里，
			# 但清单里的路径是以**安装目录**为基准的（resources/xxx、小鲸鱼.exe）。
			# 以前直接把 resources 当归基准，_self_in_package 永远比对上不，
			# 于是「用安装包装完」被误判成篡改（复测报告 4.1）。这里上提一层。
			if os.path.basename(base).lower() == "resources":
				return os.path.dirname(base)
			return base
		# v1.9.7.1（P0）：安装包的清单在 <安装目录>\resources\manifest.sha256。
		# 只看 base 自己有没有清单的话，安装形态永远找不到包根，verify_program_dir
		# 会直接报「向上找了 3 层都没找到签名清单」（复测报告 4.1 的另一半）。
		if os.path.isfile(os.path.join(base, "resources", MANIFEST)):
			return base
		parent = os.path.dirname(base)
		if parent == base:
			break
		base = parent
	return ""


def _manifest_rels(root):
	"""读出（已验签的）清单里的相对路径列表。"""
	# v1.9.7：安装包的清单装在 <安装目录>\resources\ 里，而 root 已经被上提到
	# 安装目录 —— 只认 root/manifest.sha256 的话，装出来的每一份都会被判篡改。
	cands = [os.path.join(root, MANIFEST),
	         os.path.join(root, "resources", MANIFEST)]
	text = ""
	for _mp in cands:
		try:
			text = io.open(_mp, encoding="utf-8").read()
			break
		except Exception:
			continue
	if not text:
		return []
	return [rel for _h, rel in _parse_manifest(text)]


def _self_in_package(root):
	"""v1.9.3：清单里必须真有「正在跑的这个程序」。

	复测报告 4.3：在安装目录下建一个 sub\ 把改过的 backend_server.exe 塞进去，
	find_package_root() 会向上借到父目录那份合法清单 —— 被校验的对象（清单）
	和执行的对象（sub 里的 exe）根本不是一回事，于是报「文件校验通过」。
	现在反过来问一句：清单里有没有我？没有就是借来的。
	"""
	import sys
	exe = os.path.abspath(sys.executable)
	rels = _manifest_rels(root)
	if not rels:
		return False
	try:
		rel = os.path.relpath(exe, root).replace("\\", "/")
	except Exception:
		return False
	if rel.startswith(".."):
		return False
	low = rel.lower()
	for r in rels:
		if str(r).replace("\\", "/").lower() == low:
			return True
	return False


def verify_program_dir(data_dir=None):
	"""给 app_ui 用：校验「程序所在目录」（清单在包根，不在 resources 里）。"""
	try:
		base = find_package_root()
		if not base:
			if _is_release():
				return {"ok": False, "signed": False, "tampered": True,
				        "missing": [], "changed": [],
				        "msg": "向上找了 3 层都没找到签名清单。发行版必须带清单，"
				               "缺了说明被删过 —— 为安全起见不启动。"}
			return {"ok": True, "signed": False, "tampered": False, "missing": [],
			        "changed": [], "msg": "源码运行，跳过校验"}
		# v1.9.3：发行版里，清单必须真包含正在跑的这个 exe。
		# 否则就是「拿父目录的清单给子目录的改版程序作保」。
		if _is_release() and not _self_in_package(base):
			import sys as _sys3
			_exe3 = os.path.basename(_sys3.executable)
			return {"ok": False, "signed": False, "tampered": True,
			        "missing": [], "changed": [],
			        "msg": ("这个程序不在安装包的清单里（%s）—— 有人把改过的程序"
			                "塞进了子目录。为安全起见不启动，请从官方渠道重新下载。"
			                % _exe3)}
		return verify(base)
	except Exception as e:
		return {"ok": False, "signed": False, "tampered": _is_release(),
		        "missing": [], "changed": [], "msg": "校验出错：%s" % e}


def status():
	"""给设置页显示的摘要。

	v1.9.1：以前这里把 ok 写死成 True，verify() 算出的失败被一把丢掉，
	前端只认 tampered 于是照常启动（复测报告 N-02）。现在原样透传。
	"""
	r = verify_program_dir()
	return {"ok": bool(r.get("ok")), "signed": r.get("signed"),
	        "tampered": r.get("tampered"),
	        "changed": r.get("changed") or [], "missing": r.get("missing") or [],
	        "msg": r.get("msg") or "", "algo": "ed25519",
	        "root": find_package_root()}
