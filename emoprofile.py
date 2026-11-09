# -*- coding: utf-8 -*-
"""用户情感画像（V1.8.12）

解决的问题：她记得「你上周说加班」，但不记得「你被说「呵呵」的时候是真生气」。
这里在 memory.db 里单独开一张 user_emotion_profile，存四类东西：

    traits       性格倾向        （慢热、嘴上硬心里软、爱开玩笑）
    landmines    雷区            （别提他前任、别催他找工作）
    expressions  情绪表达方式    （说「呵呵」通常是生气、说「随便」是有点不高兴）
    notes        观察记录        （滚动追加，一行一条带日期）

写入有两条路：
    1. 每天的自省线程（subconscious.daily_reflection）调 reflect_today()
       —— 让模型看当天的聊天，提炼一句「我观察到…」，并更新上面四类。
    2. 调用方手动 save()（比如以后做设置页里的人工校正）。

读出只有一条路：prompt_block(user_id)，拼进回复用的 prompt 里（不稳那段）。
没数据时返回空串，不占 token。
"""
import json
import os
import re
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_emotion_profile(
    user_id     TEXT PRIMARY KEY,
    traits      TEXT DEFAULT '',
    landmines   TEXT DEFAULT '',
    expressions TEXT DEFAULT '',
    notes       TEXT DEFAULT '',
    mood_bias   TEXT DEFAULT '',
    samples     INTEGER DEFAULT 0,
    updated_at  REAL DEFAULT 0
)
"""

_PROMPT = """你要分析「对方」（一个和你聊天的人）的情绪画像。别写诗，只写能用的观察。

下面是他/她最近的聊天记录和情绪判定记录：
{dialog}

请只输出一个 JSON（不要代码块、不要解释）：
{{"traits": "性格倾向，≤40 字", "landmines": "聊什么容易炸，≤40 字，没有就空串",
 "expressions": "他/她怎么表达不高兴/高兴，比如「说呵呵=生气」，≤60 字",
 "mood_bias": "最近整体基调，≤12 字，比如「偏累」「心情不错」",
 "note": "今天最值得记住的一句观察，≤60 字，像人说话"}}

要求：
- 只写你从记录里真能看出来的，不要编。
- note 要具体到事：「提到前任时语气变冷」，不要「情绪不稳定」这种空话。
- 没有把握的字段就留空串。"""


def _cfg_mod():
	import config as c
	return c


def _ndef(v, d):
	if v is None:
		return d
	if isinstance(v, str) and not v.strip():
		return d
	return v


# ---------------- 库连接 ----------------

def db_path():
	"""跟主记忆库同一个文件（memory.db），不另开库。"""
	try:
		import memory as _m
		p = getattr(_m, "_default_db_path", None)
		if callable(p):
			return p()
	except Exception:
		pass
	try:
		import config as c
		root = getattr(c, "DATA_ROOT", "") or ""
		return os.path.join(root, "memory", "memory.db")
	except Exception:
		return ""


def ensure_schema(conn):
	"""由 memory.Memory.__init__ 调用：老库只加表，不动数据。"""
	conn.execute(_SCHEMA)
	conn.commit()
	return True


def _conn(write=False):
	import sqlite3
	p = db_path()
	if not p:
		return None
	if not os.path.exists(p):
		if not write:
			return None
		# 写的时候允许先建库（第一次用画像、记忆库还没建出来时）
		try:
			d = os.path.dirname(p)
			if d and not os.path.isdir(d):
				os.makedirs(d, exist_ok=True)
		except Exception:
			return None
	try:
		if write:
			return sqlite3.connect(p, timeout=5.0)
		return sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro",
							   uri=True, timeout=3.0)
	except Exception:
		return None


# ---------------- 读写 ----------------

FIELDS = ("traits", "landmines", "expressions", "notes", "mood_bias")


def load(user_id):
	uid = str(user_id or "")
	if not uid:
		return {}
	conn = _conn()
	if conn is None:
		return {}
	try:
		cur = conn.execute(
			"SELECT user_id, traits, landmines, expressions, notes, mood_bias,"
			" samples, updated_at FROM user_emotion_profile WHERE user_id=?", (uid,))
		r = cur.fetchone()
		if not r:
			return {}
		return {"user_id": r[0], "traits": r[1] or "", "landmines": r[2] or "",
				"expressions": r[3] or "", "notes": r[4] or "",
				"mood_bias": r[5] or "", "samples": int(r[6] or 0),
				"updated_at": float(r[7] or 0)}
	finally:
		try:
			conn.close()
		except Exception:
			pass


def save(user_id, patch):
	uid = str(user_id or "")
	if not uid or not isinstance(patch, dict):
		return {"ok": False, "msg": "没有 user_id"}
	conn = _conn(write=True)
	if conn is None:
		return {"ok": False, "msg": "找不到记忆库"}
	try:
		conn.execute(_SCHEMA)
		cur = conn.execute("SELECT user_id FROM user_emotion_profile WHERE user_id=?", (uid,))
		old = load(uid)
		vals = {k: str(patch.get(k, old.get(k, "")) or "")[:2000] for k in FIELDS}
		samples = int(old.get("samples") or 0) + int(patch.get("samples_delta") or 0)
		if cur.fetchone():
			conn.execute(
				"UPDATE user_emotion_profile SET traits=?, landmines=?, expressions=?,"
				" notes=?, mood_bias=?, samples=?, updated_at=? WHERE user_id=?",
				(vals["traits"], vals["landmines"], vals["expressions"],
				 vals["notes"], vals["mood_bias"], samples, time.time(), uid))
		else:
			conn.execute(
				"INSERT INTO user_emotion_profile(user_id, traits, landmines, expressions,"
				" notes, mood_bias, samples, updated_at) VALUES(?,?,?,?,?,?,?,?)",
				(uid, vals["traits"], vals["landmines"], vals["expressions"],
				 vals["notes"], vals["mood_bias"], samples, time.time()))
		conn.commit()
		out = load(uid)
		out["ok"] = True
		return out
	except Exception as e:
		return {"ok": False, "msg": str(e)[:120]}
	finally:
		try:
			conn.close()
		except Exception:
			pass


def clear(user_id):
	uid = str(user_id or "")
	if not uid:
		return {"ok": False, "msg": "没有 user_id"}
	conn = _conn(write=True)
	if conn is None:
		return {"ok": False, "msg": "找不到记忆库"}
	try:
		conn.execute(_SCHEMA)
		conn.execute("DELETE FROM user_emotion_profile WHERE user_id=?", (uid,))
		conn.commit()
		return {"ok": True, "msg": "已清空这个人的情感画像"}
	except Exception as e:
		return {"ok": False, "msg": str(e)[:120]}
	finally:
		try:
			conn.close()
		except Exception:
			pass


def all_profiles(limit=50):
	conn = _conn()
	if conn is None:
		return []
	try:
		conn.execute(_SCHEMA)
		cur = conn.execute(
			"SELECT user_id, traits, landmines, expressions, notes, mood_bias,"
			" samples, updated_at FROM user_emotion_profile"
			" ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(limit or 50), 500)),))
		return [{"user_id": r[0], "traits": r[1] or "", "landmines": r[2] or "",
				 "expressions": r[3] or "", "notes": r[4] or "",
				 "mood_bias": r[5] or "", "samples": int(r[6] or 0),
				 "updated_at": float(r[7] or 0)} for r in cur.fetchall()]
	except Exception:
		return []
	finally:
		try:
			conn.close()
		except Exception:
			pass


# ---------------- 拼进 prompt ----------------

def prompt_block(user_id, max_chars=600):
	"""给回复用的那一小段。没画像就返回空串（不占 token）。"""
	try:
		if not bool(_cfg_mod().get("emotion_profile_enabled", True)):
			return ""
	except Exception:
		pass
	p = load(user_id)
	if not p:
		return ""
	bits = []
	if p.get("traits"):
		bits.append("性格：" + p["traits"])
	if p.get("mood_bias"):
		bits.append("最近基调：" + p["mood_bias"])
	if p.get("expressions"):
		bits.append("他/她的情绪表达：" + p["expressions"])
	if p.get("landmines"):
		bits.append("别踩的雷：" + p["landmines"])
	notes = [x.strip() for x in str(p.get("notes") or "").splitlines() if x.strip()]
	if notes:
		bits.append("最近观察：\n- " + "\n- ".join(notes[-3:]))
	if not bits:
		return ""
	body = "\n".join(bits)
	if len(body) > max_chars:
		body = body[:max_chars]
	return ("【你摸透的这个人】（别直接复述这些词，用它决定怎么说）\n" + body)


# ---------------- 提炼 ----------------

def _parse_json(text):
	s = str(text or "").strip()
	s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
	try:
		d = json.loads(s)
	except Exception:
		m = re.search(r"\{.*\}", s, re.S)
		if not m:
			return None
		try:
			d = json.loads(m.group(0))
		except Exception:
			return None
	return d if isinstance(d, dict) else None


def _session_of(user_id):
	return "private_" + str(user_id)


def build_dialog(mem, user_id, turns=40, max_chars=3000):
	"""拼出给模型看的材料：聊天记录 + 情绪判定记录。"""
	lines = []
	try:
		rows = mem.get_recent_messages(_session_of(user_id), turns=turns) or []
		for r in rows:
			c = str(r.get("content") or "").strip()
			if not c or c.startswith("["):
				continue
			who = "对方" if r.get("role") == "user" else "我"
			lines.append(who + "：" + c[:100])
	except Exception:
		pass
	emo = []
	try:
		import emotion_engine as _ee
		for r in (_ee.log_rows(40) or []):
			if str(r.get("scope") or "") and str(user_id) not in str(r.get("scope") or ""):
				continue
			emo.append("%s → %s(%s)" % (str(r.get("text") or "")[:40],
									   r.get("emotion") or "", r.get("weight") or ""))
	except Exception:
		pass
	body = "\n".join(lines[-turns:])
	if emo:
		body += "\n\n情绪判定记录：\n" + "\n".join(emo[-12:])
	return body[:max_chars]


def build_profile(mem, user_id, ask, day=None):
	"""让模型看今天的记录，提炼画像。ask(text) -> 模型原文。"""
	uid = str(user_id or "")
	if not uid or ask is None:
		return {"ok": False, "msg": "没有材料"}
	dialog = build_dialog(mem, uid)
	if len(dialog) < 40:
		return {"ok": False, "msg": "记录太少，先多聊几句"}
	try:
		raw = ask(_PROMPT.format(dialog=dialog))
	except Exception as e:
		return {"ok": False, "msg": "模型没回：" + str(e)[:80]}
	d = _parse_json(raw)
	if not d:
		return {"ok": False, "msg": "模型没按格式给"}
	old = load(uid)
	patch = {"traits": str(d.get("traits") or old.get("traits") or "")[:300],
			 "landmines": str(d.get("landmines") or old.get("landmines") or "")[:300],
			 "expressions": str(d.get("expressions") or old.get("expressions") or "")[:400],
			 "mood_bias": str(d.get("mood_bias") or old.get("mood_bias") or "")[:60]}
	note = str(d.get("note") or "").strip()[:200]
	if note:
		stamp = day or time.strftime("%Y-%m-%d")
		notes = [x for x in str(old.get("notes") or "").splitlines() if x.strip()]
		notes.append(stamp + " " + note)
		patch["notes"] = "\n".join(notes[-20:])
	else:
		patch["notes"] = old.get("notes") or ""
	res = save(uid, patch)
	res["msg"] = "已更新" + (("：" + note) if note else "")
	return res


def reflect_today(mem, ask=None, log=None):
	"""自省线程每天调一次：把所有有记录的账号都过一遍。"""
	def _log(m, lv="INFO"):
		if log:
			try:
				log(m, lv)
			except Exception:
				pass
	try:
		if not bool(_cfg_mod().get("emotion_profile_enabled", True)):
			return {"ok": True, "count": 0, "msg": "情感画像关着"}
	except Exception:
		pass
	if ask is None or mem is None:
		return {"ok": False, "msg": "没有模型"}
	uids = []
	try:
		rows = mem.conn.execute(
			"SELECT DISTINCT user_id FROM perm_memory WHERE user_id != ''").fetchall()
		uids = [str(r[0]) for r in rows if r and r[0]]
	except Exception:
		uids = []
	if not uids:
		return {"ok": True, "count": 0, "msg": "还没有可提炼的账号"}
	done, skipped = 0, 0
	for uid in uids[:12]:
		try:
			r = build_profile(mem, uid, ask)
			if r.get("ok"):
				done += 1
				_log("[情感画像] " + uid + " " + str(r.get("msg") or "")[:60])
			else:
				skipped += 1
				_log("[情感画像] " + uid + " 跳过：" + str(r.get("msg") or "")[:60], "DEBUG")
		except Exception as e:
			skipped += 1
			_log("[情感画像] %s 提炼失败: %s" % (uid, e), "WARN")
	return {"ok": True, "count": done, "skipped": skipped,
			"msg": "更新了 %d 个账号的情感画像" % done}
