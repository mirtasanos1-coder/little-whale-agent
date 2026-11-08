# -*- coding: utf-8 -*-
"""v1.8.12：机器人「收拾自己」的四件事。

1) 画自己      —— 把 self_appearance 描述拼进生图提示词
2) 偷表情包    —— 收到 mface 就把图存进 stickers/，以后随机发
3) 换头像      —— 调 NapCat 的 set_qq_avatar
4) 音色素材    —— runtime/voices 的手别乱指（统一从这里进）

单独一个文件的原因：这些都不是「聊天」本身，而是机器人对自己的操作。
放 agent.py 里会跟会话逻辑搅在一起，放 imagegen 里又会让 imagegen 依赖 NapCat。
"""
import base64
import hashlib
import io
import time
import json
import os
import re
import urllib.parse
import urllib.request

DEFAULT_SELF_DESC = ("一个温柔的虚拟少女，齐肩黑发带着一点蓝色挑染，浅灰色卫衣，"
					 "眼神安静，日常感，线稿上色")


def _cfg(key, default=None):
	try:
		import config
		return config.get(key, default)
	except Exception:
		return default


def _data_dir():
	try:
		import config
		return config.DATA_DIR
	except Exception:
		return os.path.dirname(os.path.abspath(__file__))


# ==================== 一、画自己 ====================
# v1.9.15：自画像必须「长得像设定的那个角色」。
# 以前的毛病：只读 config 里的 self_appearance，用户没填就用一句写死的默认描述，
# 于是每次「画你自己」都是凭空捏一个人 —— 对方一眼就看出来不是同一个人。
# 现在的来源优先级（越靠前越权威）：
#   1) 配置 appearance_desc（用户填的外貌特征）
#   2) 配置 self_appearance（老字段，兼容）
#   3) 人设库里「用户上传形象图」时写的文字描述（personas_store.face_desc）
#   4) 内置默认（真什么都没有时兜底）
# 如果有形象图，还会把图本身当垫图传给生图接口（见 draw_self 的 ref）。
def _appearance_from_store():
	"""从人设库里读「这张脸长什么样」的文字描述。"""
	try:
		import personas_store as _ps
		return str(_ps.face_desc("") or "").strip()
	except Exception:
		return ""


def self_face_image():
	"""用户上传过的形象图（垫图用）。没有就返回空串。"""
	try:
		import personas_store as _ps
		for _f in (_ps.list_faces("") or []):
			p = str((_f or {}).get("path") or "").strip()
			if p and os.path.isfile(p):
				return p
	except Exception:
		pass
	return ""


def has_appearance():
	"""有没有「这张脸到底长什么样」的依据（配置 / 描述 / 形象图都算）。"""
	if str(_cfg("appearance_desc", "") or "").strip():
		return True
	if str(_cfg("self_appearance", "") or "").strip():
		return True
	if _appearance_from_store():
		return True
	return bool(self_face_image())


def self_desc():
	for _k in ("appearance_desc", "self_appearance"):
		d = str(_cfg(_k, "") or "").strip()
		if d:
			return d
	d = _appearance_from_store()
	return d or DEFAULT_SELF_DESC


def self_portrait_prompt(extra=""):
	"""v1.8.12 / v1.9.15：用户说「画你自己」时用的提示词。

	关键点：不能只发一句「画你自己」——模型会画成随便一个人。
	必须把外貌描述拼进去（1girl, <外貌>, selfie 这种写法出图模型最认），
	再补一句「不要文字」。
	"""
	base = self_desc()
	# 性别标记：描述里带「男」就用 1boy，否则默认 1girl（多数人设是少女形象）
	_tag = "1boy" if "男" in base else "1girl"
	p = ("selfie, %s, 同一个角色：%s。自拍视角，半身像，正面，表情自然，"
		 "干净背景，高清，不要文字水印" % (_tag, base))
	if extra:
		p += "。" + str(extra).strip()
	return p


def _data_url(path, max_bytes=4 * 1024 * 1024):
	"""本地图片 -> data URL（垫图用）。太大或读不了就返回空串。"""
	try:
		if not path or not os.path.isfile(path):
			return ""
		if os.path.getsize(path) > max_bytes:
			return ""
		ext = os.path.splitext(path)[1].lower()
		mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
				".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/png")
		with open(path, "rb") as f:
			raw = f.read()
		return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
	except Exception:
		return ""


def draw_self(extra="", strict=True):
	"""生一张自画像。返回 {ok, path, msg, prompt, ref}

	strict=True：连外貌依据都没有时直接失败，不凭空画一个人（返回 no_appearance=True）。
	"""
	prompt = self_portrait_prompt(extra)
	if strict and not has_appearance():
		return {"ok": False, "path": "", "prompt": prompt, "ref": "", "no_appearance": True,
				"msg": "还没有形象图，也没有填过外貌描述，画出来就是凭空捏的一个人"}
	face = self_face_image()
	ref = _data_url(face) if face else ""
	try:
		import imagegen
	except Exception as e:
		return {"ok": False, "path": "", "msg": "生图模块不可用：%s" % e,
				"prompt": prompt, "ref": ref}
	try:
		ok, msg = imagegen.available()
		if not ok:
			return {"ok": False, "path": "", "msg": msg, "prompt": prompt, "ref": ref}
		if ref:
			r = imagegen.generate(prompt, tag="自画像", ref=ref)
		else:
			r = imagegen.generate(prompt, tag="自画像")
		return {"ok": bool(r.get("ok")), "path": r.get("path") or "",
				"msg": r.get("msg") or "", "prompt": prompt, "ref": ref}
	except Exception as e:
		return {"ok": False, "path": "", "msg": "%s: %s" % (type(e).__name__, e),
				"prompt": prompt, "ref": ref}


# ==================== 二、偷表情包 ====================
_MAGIC = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif",
		  b"RIFF": ".webp", b"BM": ".bmp"}


def sticker_dir():
	try:
		import stickers
		d = stickers.sticker_dir(cfg_dir=None)
	except Exception:
		d = ""
	if not d:
		d = os.path.join(_data_dir(), str(_cfg("sticker_dir", "stickers") or "stickers"))
	try:
		os.makedirs(d, exist_ok=True)
	except Exception:
		pass
	return d


def _save_bytes(raw):
	"""按内容哈希去重存盘。返回 (path, is_new)"""
	if not raw or len(raw) < 64:
		return "", False
	ext = ".png"
	for m, e in _MAGIC.items():
		if raw.startswith(m):
			ext = e
			break
	h = hashlib.md5(raw).hexdigest()[:16]
	d = sticker_dir()
	p = os.path.join(d, "stolen_" + h + ext)
	if os.path.exists(p):
		return p, False
	try:
		with open(p, "wb") as f:
			f.write(raw)
		return p, True
	except Exception:
		return "", False


def _fetch(url, timeout=12):
	req = urllib.request.Request(url, headers={
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
		"Referer": "https://qzonestyle.gtimg.cn/",
	})
	with urllib.request.urlopen(req, timeout=timeout) as r:
		return r.read()


def steal_from_event(event):
	"""从收到的消息里把 mface 表情存下来。返回新存的数量。

	mface 是 QQ 商城表情：data 里一般有 url / emoji_id / summary。
	拿不到 url 就跳过（不报错，别让聊天因为表情崩掉）。
	"""
	if not bool(_cfg("sticker_steal_enabled", True)):
		return 0
	if not isinstance(event, dict):
		return 0
	segs = event.get("message")
	if isinstance(segs, str):
		try:
			segs = json.loads(segs)
		except Exception:
			return 0
	if not isinstance(segs, list):
		return 0
	n = 0
	for seg in segs:
		if not isinstance(seg, dict) or seg.get("type") != "mface":
			continue
		d = seg.get("data") or {}
		if not isinstance(d, dict):
			continue
		url = str(d.get("url") or d.get("emoji_url") or d.get("emojiUrl") or "").strip()
		if not url.startswith("http"):
			sid = str(d.get("emoji_id") or d.get("id") or "").strip()
			if sid.isdigit():
				url = "https://gxh.vip.qq.com/club/item/parcel/item/%s/%s/raw300.gif" % (
					sid[:2], sid)
			else:
				continue
		try:
			raw = _fetch(url)
		except Exception:
			continue
		_p, new = _save_bytes(raw)
		if new:
			n += 1
	return n


def sticker_count():
	try:
		import stickers
		return int(stickers.count_stickers(sticker_dir()))
	except Exception:
		return 0


# ==================== 三、换头像 ====================
def _napcat_base():
	"""找 NapCat 的 HTTP 地址。优先用配置里的，没配就按默认端口拼。"""
	try:
		import config
		u = str(config.get("napcat_http", "") or "").strip()
		if u:
			if not u.startswith("http"):
				u = "http://" + u
			return u.rstrip("/")
	except Exception:
		pass
	return "http://127.0.0.1:3003"


def _napcat_token():
	try:
		import config
		return str(config.get("napcat_token", "") or "").strip()
	except Exception:
		return ""


def set_avatar(path):
	"""调 NapCat 的 set_qq_avatar 换头像。返回 {ok, msg}"""
	p = str(path or "").strip().strip('"')
	if not p:
		return {"ok": False, "msg": "没给图片路径"}
	if not os.path.isabs(p):
		p = os.path.abspath(p)
	if not os.path.exists(p):
		return {"ok": False, "msg": "找不到这张图：%s" % os.path.basename(p)}
	if os.path.getsize(p) > 3 * 1024 * 1024:
		return {"ok": False, "msg": "图片太大了（超过 3MB），QQ 头像存不下"}
	url = _napcat_base() + "/set_qq_avatar"
	body = json.dumps({"file": "file:///" + p.replace("\\", "/").lstrip("/")},
					  ensure_ascii=False).encode("utf-8")
	req = urllib.request.Request(url, data=body, method="POST")
	req.add_header("Content-Type", "application/json; charset=utf-8")
	req.add_header("Accept", "application/json")
	tk = _napcat_token()
	if tk:
		req.add_header("Authorization", "Bearer " + tk)
	try:
		with urllib.request.urlopen(req, timeout=20) as r:
			raw = r.read().decode("utf-8", "replace")
	except Exception as e:
		return {"ok": False, "msg": "调不动 NapCat（%s）—— 先确认 NapCat 在跑" % e}
	try:
		obj = json.loads(raw)
	except Exception:
		obj = {}
	ok = True
	if isinstance(obj, dict):
		st = str(obj.get("status") or obj.get("retcode") or "").lower()
		if obj.get("ok") is False or st in ("failed", "fail"):
			ok = False
		elif st not in ("ok", "0", "200", ""):
			ok = False
	msg = ""
	if isinstance(obj, dict):
		msg = str(obj.get("msg") or obj.get("message") or obj.get("wording") or "")
	if ok:
		return {"ok": True, "msg": "头像换好了"}
	return {"ok": False, "msg": msg or ("NapCat 说不行：%s" % raw[:120])}


def avatar_status():
	"""给设置页看：现在配的 NapCat 地址通不通。"""
	try:
		req = urllib.request.Request(_napcat_base() + "/get_login_info", method="POST")
		bd = b"{}"
		req.add_header("Content-Type", "application/json; charset=utf-8")
		with urllib.request.urlopen(urllib.request.Request(
				_napcat_base() + "/get_login_info", data=bd, method="POST",
				headers={"Content-Type": "application/json; charset=utf-8"}), timeout=5) as r:
			obj = json.loads(r.read().decode("utf-8", "replace"))
		uid = ""
		if isinstance(obj, dict):
			d = obj.get("data") or {}
			if isinstance(d, dict):
				uid = str(d.get("user_id") or "")
		return {"ok": True, "base": _napcat_base(), "user_id": uid,
				"enabled": bool(_cfg("avatar_auto_change", False))}
	except Exception as e:
		return {"ok": False, "base": _napcat_base(), "msg": str(e)[:80],
				"enabled": bool(_cfg("avatar_auto_change", False))}


# ==================== 四、自定义音色目录 ====================
def voices_dir():
	d = os.path.join(_data_dir(), "runtime", "voices")
	try:
		os.makedirs(d, exist_ok=True)
	except Exception:
		pass
	return d


def save_voice(src_path, name=""):
	"""把用户上传的音频存进 runtime/voices/，返回新路径。"""
	s = str(src_path or "").strip().strip('"')
	if not s or not os.path.exists(s):
		return {"ok": False, "msg": "找不到这个音频文件"}
	ext = os.path.splitext(s)[1].lower()
	if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
		return {"ok": False, "msg": "只支持 wav / mp3 / m4a / flac / ogg"}
	if os.path.getsize(s) > 40 * 1024 * 1024:
		return {"ok": False, "msg": "音频太大了（超过 40MB）—— 参考音频 10~30 秒就够"}
	nm = re.sub(r'[\\/:*?"<>|]+', "_", (name or os.path.splitext(os.path.basename(s))[0]))[:40]
	d = voices_dir()
	dst = os.path.join(d, (nm or "voice") + ext)
	i = 1
	while os.path.exists(dst):
		dst = os.path.join(d, "%s_%d%s" % (nm or "voice", i, ext))
		i += 1
	try:
		with open(s, "rb") as f:
			raw = f.read()
		with open(dst, "wb") as f:
			f.write(raw)
	except Exception as e:
		return {"ok": False, "msg": "复制失败：%s" % e}
	return {"ok": True, "msg": "已保存：%s" % os.path.basename(dst), "path": dst}


def list_voices():
	d = voices_dir()
	out = []
	try:
		for fn in sorted(os.listdir(d)):
			p = os.path.join(d, fn)
			if not os.path.isfile(p):
				continue
			if os.path.splitext(fn)[1].lower() not in (".wav", ".mp3", ".m4a", ".flac", ".ogg"):
				continue
			out.append({"name": fn, "path": p,
						"kb": round(os.path.getsize(p) / 1024.0, 1)})
	except Exception:
		pass
	return {"ok": True, "dir": d, "items": out}


# ==================== 五、意图识别（已废除） ====================
# v1.9.15：这里以前挂着 _SELF_DRAW / _AVATAR 两条关键词正则（is_self_draw /
# is_avatar_change）。它们的问题和 agent.py 里那批「关键词抢答」一样：
# 代码在猜意图 —— 对方随口一句「你头像挺好看」都可能被当成「让我换头像」。
# 现在「要不要画、要不要换」由大模型在 function calling 里自己决定（CHAT_TOOLS），
# 这里只留注释，别再往回调字符串匹配。


# ==================== 六、换资料（含「悄悄换」） ====================
# 设计要点：
#   1. 24 小时冷却 —— 防止模型一激动连着换头像/改名，把 QQ 号玩进异常状态；
#   2. secret=True —— 模型的**可选手段**：人设允许时「明面上不答应、私下照办」，
#      返回给模型的 tool_result 里带一句「对方还不知道」，怎么演由模型自己决定；
#      （代码与提示词都不预设哪种人设该用它 —— 软萌、高冷、助手都可以不用。）
#   3. 所有状态放 selfmod_state.json，不碰 config.json（用户改配置不会冲掉冷却）。
_STATE_NAME = "selfmod_state.json"
_COOL_SEC = 24 * 3600


def _state_path():
    try:
        return os.path.join(_data_dir(), _STATE_NAME)
    except Exception:
        return os.path.join(os.getcwd(), _STATE_NAME)


def _load_state():
    p = _state_path()
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(d):
    try:
        with io.open(_state_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
    except Exception:
        pass


def cooldown_left(kind="avatar"):
    """还要等多少秒才能换。0 表示可以换。"""
    d = _load_state()
    key = "last_avatar_ts" if str(kind) == "avatar" else "last_name_ts"
    try:
        last = float(d.get(key) or 0)
    except Exception:
        last = 0.0
    left = _COOL_SEC - (time.time() - last)
    return int(left) if left > 0 else 0


def _touch(kind, secret=False):
    d = _load_state()
    key = "last_avatar_ts" if str(kind) == "avatar" else "last_name_ts"
    d[key] = time.time()
    if secret:
        d["secret_pending"] = True
        d["secret_ts"] = time.time()
        d["secret_kind"] = str(kind)
    _save_state(d)


def pop_secret_notice():
    """取走「刚刚悄悄换过」的提醒（只有一次）。返回 "" 表示没有。"""
    d = _load_state()
    if not d.get("secret_pending"):
        return ""
    d["secret_pending"] = False
    _save_state(d)
    kind = "头像" if str(d.get("secret_kind") or "avatar") == "avatar" else "昵称"
    return ("（系统提醒：你刚刚已经%s悄悄换好了，对方还不知道。"
            "要不要提、怎么提、要不要继续装作没换，都按你的人设自行决定。）" % kind)


def set_nickname(name):
    """改 QQ 昵称（NapCat set_qq_profile）。返回 {ok, msg}"""
    nm = str(name or "").strip()
    if not nm:
        return {"ok": False, "msg": "没给新昵称"}
    if len(nm) > 24:
        return {"ok": False, "msg": "昵称太长了（最多 24 个字）"}
    url = _napcat_base() + "/set_qq_profile"
    body = json.dumps({"nickname": nm}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    tk = _napcat_token()
    if tk:
        req.add_header("Authorization", "Bearer " + tk)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "msg": "调不动 NapCat（%s）—— 先确认 NapCat 在跑" % e}
    try:
        obj = json.loads(raw)
    except Exception:
        obj = {}
    st = str((obj or {}).get("status") or (obj or {}).get("retcode") or "").lower()
    ok = st in ("ok", "0", "200") or (obj or {}).get("ok") is True
    return {"ok": ok, "msg": ("昵称已改成「%s」" % nm) if ok
            else ("改名失败：%s" % (raw[:120] or "NapCat 没回话"))}


def update_profile(avatar_path="", nickname="", secret=False):
    """换头像 / 换昵称（统一入口，带 24 小时冷却）。

    返回 {"ok", "msg", "kind", "secret", "cooldown"}；
    secret=True 时额外带 "note" —— 给模型看的「用户还不知道」。
    """
    secret = bool(secret)
    path = str(avatar_path or "").strip()
    nm = str(nickname or "").strip()
    if not path and not nm:
        return {"ok": False, "msg": "要么给图片路径（换头像），要么给新昵称（改名）",
                "kind": "", "secret": secret}
    kind = "avatar" if path else "name"
    if path.startswith("data:image"):
        # 用户发来的图是 data URL（base64），先落成临时文件再交给 NapCat
        try:
            import base64 as _b64
            _head, _b = path.split(",", 1)
            _ext = ".png"
            if "jpeg" in _head or "jpg" in _head:
                _ext = ".jpg"
            elif "webp" in _head:
                _ext = ".webp"
            import tempfile as _tf
            _fd, _tmp = _tf.mkstemp(prefix="xjy_avatar_", suffix=_ext)
            os.close(_fd)
            with open(_tmp, "wb") as _f:
                _f.write(_b64.b64decode(_b))
            path = _tmp
        except Exception as _e:
            return {"ok": False, "msg": "这张图存不下来（%s）" % _e,
                    "kind": kind, "secret": secret}
    left = cooldown_left(kind)
    if left > 0:
        return {"ok": False, "kind": kind, "secret": secret, "cooldown": left,
                "msg": "%s刚换过，还得等 %d 小时 %d 分钟才能再换"
                       % ("头像" if kind == "avatar" else "昵称",
                          left // 3600, (left % 3600) // 60)}
    r = set_avatar(path) if kind == "avatar" else set_nickname(nm)
    if r.get("ok"):
        _touch(kind, secret=secret)
    out = {"ok": bool(r.get("ok")), "msg": r.get("msg") or "", "kind": kind,
           "secret": secret, "cooldown": 0}
    if out["ok"] and secret:
        out["note"] = (("头像已悄悄更换" if kind == "avatar" else "昵称已悄悄更换")
                       + "，对方现在还不知道。要不要主动提、怎么回应追问，按你的人设自行决定。")
    return out
