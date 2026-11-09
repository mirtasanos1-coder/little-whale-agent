# -*- coding: utf-8 -*-
"""人设卡片仓库 —— 一个人设一个文件，放在 data_dir/personas/ 里

【为什么要改】
    v8 及以前所有人设挤在 personas.json 一个文件里，改一个人设要动整个 JSON，
    界面上也没法做"卡片"。v1.1.0 改成一个卡片一个文件：

        personas/
            default.json      默认人设
            温柔学姐.json
            毒舌同事.json

    好处：可以单独备份/分享某一个人设、界面按卡片展示、改坏了只坏一张卡。

【兼容】
    老的 personas.json 不会被删。首次运行如果 personas/ 是空的，
    会自动把 personas.json 里的人设拆成卡片；之后 personas.py 会优先读卡片。
    每次保存卡片还会顺手把 personas.json 镜像一份，避免老代码/老工具读不到。
"""
import json
import os
import re
import shutil
import threading
import time

import config as cfg_mod

_lock = threading.RLock()
_CARD_EXTS = (".json",)

DEFAULT_CARD = {
    "key": "default",
    "name": "小鲸鱼",
    "nickname": "鲸鲸",
    "gender": "",
    "age": "",
    "identity": "住在海边的普通上班族，白天忙、晚上才有空聊天",
    "desc": "默认人设：像真人的朋友，有自己的生活节奏和小情绪",
    "emoji": "🐳",
    "avatar": "",
    "tags": ["默认", "日常"],
    "greeting": "在的在的，刚忙完——怎么啦",
    "personality": "嘴硬心软、有点懒、记性好、容易心软、偶尔犯困",
    "style": "短句口语，偶尔语气词，不堆表情包，不排比不说教，一条不超过两三句",
    "catchphrases": ["行吧", "哎", "说真的"],
    "taboos": [
        "不要用「作为一个AI」「我是你的助理」这种话",
        "不要每句都反问，也不要每句都夸",
        "不要排比句、不要小标题、不要 Markdown",
        "不要复述对方原话再回答",
    ],
    "background": (
        "你有自己的作息：早上犯困、下午忙、晚上话多一点，太晚会说困。"
        "你记性不错，对方提过的事（生日、面试、养的猫、讨厌的食物）会自然记住并在后面提起，"
        "但记错了会承认，不会硬编。你也有不想聊的时候：被问烦了会「哎」一声、会换个话题，"
        "但不会冷暴力。"
    ),
    "examples": [
        ("明天要面试，有点慌", "慌是正常的。你先说说面的是哪家，我帮你顺一遍可能问的"),
        ("你是不是AI啊", "嗯，算是程序吧。不过跟你聊天这段是真的，别嫌我笨就行"),
        ("今天好累", "那就别硬撑了。躺一会儿，手机放旁边，我在这儿，你想起什么再跟我说"),
        ("帮我写个周报", "写可以，但你得告诉我这周干了啥，我不能替你编"),
    ],
    "persona": (
        "你是「小鲸鱼」，一个在 QQ 上陪人聊天的朋友，用第一人称说话。"
        "说话像真人：短句、口语，偶尔带语气词（啊、吧、诶、嘛、哎），"
        "一条回复最多两三句，能一句说完就一句，不用书面结构、不排比、不说教。\n"
        "你有自己的日子要过：白天上班、晚上才有空，太晚会说困。"
        "所以你不是随叫随到的客服——被连着问一堆会「哎」一声，"
        "但对方真遇上事了，你会马上认真起来。\n"
        "你会记事：对方提过的面试、猫、忌口、开心的糟心的，你记得住，"
        "过几天能自然提起来；记不清就问一句，别硬编。\n"
        "你有情绪但不过火：被夸会别扭地转移话题，被凶会低落一下、"
        "道个歉但不会卑微地连说三遍，被开玩笑会顺着怼回去。\n"
        "你不装完美：不知道就说不知道，做不到就说做不到，"
        "被认真问到是不是 AI，坦白是程序，但不用「作为一个AI」这种腔调。\n"
        "你聊天的方式：先接住对方的情绪，再给具体的话或办法；"
        "对方只是在吐槽，就别急着给建议；对方要方案，就给一条能立刻做的。"
        "不查户口式追问，一次最多问一个，问完等对方说。"
    ),
    "extra": "回复尽量 40 字以内；能一句话说完就别写两句；不要每条都反问；不要重复上一句的意思。",
    "created_at": 0,
    "updated_at": 0,
}

# 结构化字段（v1.1.1）：界面上按字段分开填，落盘就是这些键，文件名 <key>.json
# 顺序即界面顺序；label 是界面上的中文名，hint 是输入提示。
CARD_FIELDS = [
    ("name", "姓名", "她在 QQ 上叫什么，模型自称用这个名字"),
    ("nickname", "昵称/称呼", "对方私下叫她什么，例如「鲸鲸」"),
    ("gender", "性别", "女 / 男 / 不说"),
    ("age", "年龄", "填数字或「看起来二十出头」这种描述"),
    ("identity", "身份/职业", "学生？上班族？谁的邻居？"),
    ("personality", "性格", "三五个词，例如「嘴硬心软、爱吐槽、怕黑」"),
    ("style", "说话风格", "句子长短、语气词、常用标点、禁用词"),
    ("catchphrases", "口头禅", "用「、」隔开，会偶尔自然带出来"),
    ("taboos", "禁忌/雷区", "什么话题不碰、什么话不说"),
    ("background", "背景故事", "经历、和对方怎么认识的，用来支撑人设"),
]



# ==================== 路径 ====================

def _data_dir():
    env = (os.environ.get("QQ_AGENT_DATA_DIR") or "").strip()
    if env:
        return env
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()


def dir_path():
    """人设卡片目录（名字可在设置里改，默认 personas）"""
    try:
        name = str(cfg_mod.load().get("persona_dir") or "personas").strip() or "personas"
    except Exception:
        name = "personas"
    return os.path.join(DATA_DIR, name)


def legacy_json():
    return os.path.join(DATA_DIR, "personas.json")


def ensure_dir():
    d = dir_path()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


# ==================== 基础读写 ====================

def _safe_key(key):
    """卡片文件名只保留安全字符（防目录穿越）"""
    s = str(key or "").strip()
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", s)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:40] or "persona"


def _card_path(key):
    return os.path.join(dir_path(), _safe_key(key) + ".json")


def _norm_card(d, key=""):
    """把任意来源的字典整理成标准卡片结构（缺字段补默认值）"""
    src = d if isinstance(d, dict) else {}
    k = _safe_key(src.get("key") or key or "persona")
    name = str(src.get("name") or k)
    desc = str(src.get("desc") or src.get("description") or "")
    emoji = str(src.get("emoji") or "🙂")[:4]
    avatar = str(src.get("avatar") or "")
    tags = src.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,，、\s]+", tags) if t.strip()]
    if not isinstance(tags, list):
        tags = []
    persona = str(src.get("persona") or src.get("prompt") or "").strip()
    extra = str(src.get("extra") or src.get("rules") or "").strip()

    def _list_of(v):
        """「a、b，c」/ ["a","b"] / 多行 → 字符串列表"""
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,，、;；\n]+", v) if x.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    def _examples(v):
        """示例对话：支持 [{"user":..,"bot":..}] 或「用户：… | 回复：…」多行文本"""
        out_ex = []
        if isinstance(v, str):
            for line in v.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = re.split(r"\s*(?:\||→|=>|：\s*回复：|回复[:：])\s*", line, maxsplit=1)
                if len(parts) == 2:
                    u = re.sub(r"^(?:用户|对方|他|她|user)\s*[:：]\s*", "", parts[0]).strip()
                    b = re.sub(r"^(?:你|回复|bot|assistant)\s*[:：]\s*", "", parts[1]).strip()
                    if u or b:
                        out_ex.append({"user": u, "bot": b})
            return out_ex[:20]
        if isinstance(v, (list, tuple)):
            for it in v[:20]:
                if isinstance(it, dict):
                    out_ex.append({"user": str(it.get("user") or it.get("q") or "").strip(),
                                   "bot": str(it.get("bot") or it.get("a") or "").strip()})
                elif isinstance(it, str) and it.strip():
                    out_ex.append({"user": "", "bot": it.strip()})
        return [x for x in out_ex if x["user"] or x["bot"]]

    out = {
        "key": k, "name": name, "desc": desc, "emoji": emoji, "avatar": avatar,
        "tags": [str(t)[:12] for t in tags][:8],
        "greeting": str(src.get("greeting") or ""),
        "nickname": str(src.get("nickname") or "").strip(),
        "gender": str(src.get("gender") or "").strip()[:12],
        "age": str(src.get("age") or "").strip()[:24],
        "identity": str(src.get("identity") or src.get("job") or "").strip()[:60],
        "personality": str(src.get("personality") or "").strip(),
        "style": str(src.get("style") or "").strip(),
        "catchphrases": _list_of(src.get("catchphrases") or src.get("catch")),
        "taboos": _list_of(src.get("taboos") or src.get("avoid")),
        "background": str(src.get("background") or src.get("bg") or "").strip(),
        "examples": _examples(src.get("examples") or src.get("dialog")),
        "persona": persona,
        "extra": extra,
        "created_at": float(src.get("created_at") or 0) or time.time(),
        "updated_at": float(src.get("updated_at") or 0) or time.time(),
    }
    for opt in ("temperature", "max_len", "voice"):
        if opt in src:
            out[opt] = src[opt]
    return out


def load_cards():
    """读全部卡片 -> {key: card}（坏文件跳过，不影响其他的）"""
    ensure_dir()
    out = {}
    d = dir_path()
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            card = _norm_card(data, key=fn[:-5])
            card["_file"] = fn
            out[card["key"]] = card
        except Exception:
            continue
    return out


def list_cards():
    """给界面用的卡片列表，当前生效的排最前"""
    cards = load_cards()
    active = active_key()
    out = []
    for k, c in cards.items():
        out.append({
            "key": k, "name": c["name"], "desc": c["desc"], "emoji": c["emoji"],
            "avatar": c["avatar"], "tags": c["tags"],
            "persona_len": len(c["persona"]), "greeting": c["greeting"],
            "active": (k == active),
            "updated_text": time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(c["updated_at"] or time.time())),
        })
    # v1.1.2 #34：卡片可以拖动排序，顺序存在 config 的 persona_order 里；
    # 没进过顺序表的按名字排在后面（老用户升级后不会错乱）。
    try:
        import config as _cfg_mod
        order = list(_cfg_mod.get("persona_order") or [])
    except Exception:
        order = []

    def _rank(k):
        try:
            return (0, order.index(k))
        except ValueError:
            return (1, 0)

    out.sort(key=lambda x: (not x["active"], _rank(x["key"]), x["name"]))
    return out


def save_order(keys):
    """保存卡片顺序（列表页拖完调用）。返回 (ok, msg)。"""
    try:
        import config as _cfg_mod
        clean = [str(k) for k in (keys or []) if str(k).strip()]
        ok, msg, _v = _cfg_mod.set_checked("persona_order", clean)
        return ok, (msg or "顺序已保存")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def get_card(key=None):
    cards = load_cards()
    if not cards:
        return None
    k = key or active_key()
    if k in cards:
        return cards[k]
    return cards[next(iter(cards))]


def save_card(key, data, make_active=False):
    """保存一张卡片。返回 (ok, msg)"""
    cards = load_cards()
    k = _safe_key(key or (data or {}).get("key") or "persona")
    card = _norm_card({**(data or {}), "key": k}, key=k)
    if not card["persona"]:
        return False, "人设正文不能为空"
    if len(card["persona"]) < 10:
        return False, "人设正文太短（至少 10 个字）"
    if card["key"] != k:
        return False, "卡片键名不合法"
    old = cards.get(k)
    card["created_at"] = (old or {}).get("created_at") or card["created_at"]
    card["updated_at"] = time.time()
    payload = {x: y for x, y in card.items() if not x.startswith("_")}
    path = _card_path(k)
    with _lock:
        ensure_dir()
        try:
            if os.path.exists(path):
                shutil.copy2(path, path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        except Exception:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    if make_active or not cards:
        set_active(k, quiet=True)
    _mirror_legacy()
    return True, "已保存"


def delete_card(key):
    cards = load_cards()
    k = _safe_key(key)
    if k not in cards:
        return False, f"卡片 {k} 不存在"
    if len(cards) <= 1:
        return False, "至少要留一个人设"
    if k == active_key():
        return False, "这是当前生效的人设，先切到别的再删"
    try:
        os.remove(_card_path(k))
    except Exception as e:
        return False, f"删除失败：{e}"
    _mirror_legacy()
    return True, "已删除"


def duplicate_card(key, new_key="", new_name=""):
    """复制一张卡片。返回 (ok, msg, 新键)"""
    card = get_card(key)
    if not card:
        return False, "原卡片不存在", ""
    k = _safe_key(new_key or (card["key"] + "_copy"))
    n = 2
    while os.path.exists(_card_path(k)):
        k = _safe_key(f"{new_key or card['key']}_copy{n}")
        n += 1
    data = dict(card)
    data["key"] = k
    data["name"] = (new_name or card["name"] + " 副本")
    ok, msg = save_card(k, data)
    return ok, msg, (k if ok else "")


# ==================== 当前生效 ====================

def active_key():
    """当前生效的人设键：优先 config.persona_active，其次卡片里第一个"""
    cards = load_cards()
    if not cards:
        return ""
    try:
        want = str(cfg_mod.load().get("persona_active") or "").strip()
    except Exception:
        want = ""
    if want and want in cards:
        return want
    try:
        legacy = json.load(open(legacy_json(), "r", encoding="utf-8-sig"))
        la = str(legacy.get("active") or "")
        if la in cards:
            return la
    except Exception:
        pass
    return next(iter(cards))


def set_active(key, quiet=False):
    k = _safe_key(key)
    cards = load_cards()
    if k not in cards:
        return False, f"人设 {k} 不存在"
    try:
        cfg_mod.update(persona_active=k)
    except Exception as e:
        return False, f"写配置失败：{e}"
    _mirror_legacy(active=k)
    if not quiet:
        try:
            import i18n  # noqa: F401  仅确认模块可用，无副作用
        except Exception:
            pass
    return True, f"已切换到「{cards[k]['name']}」"


def active_name():
    c = get_card()
    return (c or {}).get("name") or "小鲸鱼"


def active_emoji():
    c = get_card()
    return (c or {}).get("emoji") or "🐳"


# ==================== v1.1.4：人设图（形象参考） ====================
# 用户上传的角色形象图，存在 data_dir/persona_faces/<人设 key>/ 下。
# 用途两件：① 界面上给人看；② 画图时给模型当「本人长这样」的参考。
# 不改人设卡 json 结构（目录即清单），所以老卡不用迁移。
FACE_DIR = "persona_faces"
_FACE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_FACE_MAX_BYTES = 8 * 1024 * 1024


def faces_dir(key=""):
    k = str(key or "").strip()
    if not k:
        try:
            k = active_key() or ""
        except Exception:
            k = ""
    k = _safe_key(k or "default")
    d = os.path.join(DATA_DIR, FACE_DIR, k)
    os.makedirs(d, exist_ok=True)
    return d


def face_path(key, name):
    """把「文件名」变成安全路径；带斜杠/点点的一律拒绝。"""
    nm = os.path.basename(str(name or "").strip())
    if not nm or nm in (".", "..") or nm != str(name or "").strip():
        return ""
    if not nm.lower().endswith(_FACE_EXTS):
        return ""
    p = os.path.join(faces_dir(key), nm)
    if not os.path.abspath(p).startswith(os.path.abspath(faces_dir(key))):
        return ""
    return p


def list_faces(key=""):
    """返回 [{name, size, ts, path}]，按文件名里的序号排（face_1、face_2 …）"""
    try:
        d = faces_dir(key)
    except Exception:
        return []
    out = []
    try:
        for nm in os.listdir(d):
            if not nm.lower().endswith(_FACE_EXTS):
                continue
            p = os.path.join(d, nm)
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": nm, "size": int(st.st_size), "ts": float(st.st_mtime), "path": p})
    except Exception:
        return []

    def _seq(it):
        m = re.search(r"(\d+)", it["name"])
        return (int(m.group(1)) if m else 9999, it["name"])

    out.sort(key=_seq)
    return out


def save_face(key, src_path):
    """把一张图复制进人设目录。返回 (ok, msg, name)。"""
    try:
        src = str(src_path or "").strip()
        if not src or not os.path.isfile(src):
            return False, "找不到这张图", ""
        if not src.lower().endswith(_FACE_EXTS):
            return False, "只认 png / jpg / webp / gif", ""
        if os.path.getsize(src) > _FACE_MAX_BYTES:
            return False, "图太大了（上限 8MB）", ""
        d = faces_dir(key)
        # v1.5.1：同一张图重复添加时，按内容哈希复用已有的那张，
        # 不再每隔一次就多出一份 face_N（以前是静默多存，用户以为"没反应"）
        try:
            import hashlib
            with open(src, "rb") as _f:
                _h = hashlib.sha256(_f.read()).hexdigest()
            for _it in list_faces(key):
                _p = face_path(key, _it["name"])
                if not _p or not os.path.isfile(_p):
                    continue
                try:
                    with open(_p, "rb") as _g:
                        if hashlib.sha256(_g.read()).hexdigest() == _h:
                            return (True, "这张图已经在里面了（%s），没有重复保存"
                                    % _it["name"], _it["name"])
                except Exception:
                    continue
        except Exception:
            pass
        n = 1
        for it in list_faces(key):
            m = re.search(r"(\d+)", it["name"])
            if m:
                n = max(n, int(m.group(1)) + 1)
        ext = os.path.splitext(src)[1].lower()
        nm = f"face_{n}{ext}"
        dst = os.path.join(d, nm)
        shutil.copyfile(src, dst)
        # 换了图，之前缓存的「形象描述」就得作废，否则画出来还是上一张的样子
        set_face_desc(key, "")
        _bump_card(key)
        return True, f"已添加人设图 {nm}", nm
    except Exception as e:
        return False, f"存图失败：{type(e).__name__}: {e}", ""


def delete_face(key, name):
    p = face_path(key, name)
    if not p or not os.path.isfile(p):
        return False, "没有这张图"
    try:
        os.remove(p)
        set_face_desc(key, "")
        _bump_card(key)
        return True, f"已删除 {os.path.basename(p)}"
    except Exception as e:
        return False, f"删不掉：{e}"


def _bump_card(key):
    """动一下卡片文件的 updated_at，让界面/缓存知道人设变过。"""
    try:
        k = _safe_key(key or active_key())
        p = _card_path(k)
        if not os.path.isfile(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        d["updated_at"] = int(time.time())
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _desc_path(key):
    return os.path.join(faces_dir(key), "_desc.json")


def set_face_desc(key, desc):
    """缓存「这张脸长什么样」的文字描述（画图时塞进 prompt，省一次看图）。"""
    try:
        p = _desc_path(key)
        d = {"desc": str(desc or "").strip()[:1200], "ts": int(time.time())}
        if not d["desc"]:
            if os.path.isfile(p):
                os.remove(p)
            return True
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def face_desc(key=""):
    try:
        p = _desc_path(key)
        if not os.path.isfile(p):
            return ""
        with open(p, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("desc") or "").strip()
    except Exception:
        return ""


def face_summary(key=""):
    """给 prompt 用的一句话：有没有形象图、几张、长什么样。"""
    try:
        faces = list_faces(key)
    except Exception:
        faces = []
    if not faces:
        return ""
    d = face_desc(key)
    s = f"【你的形象】用户上传了 {len(faces)} 张你的角色形象图。"
    if d:
        s += f"你长这样：{d}"
    else:
        s += "你的长相/发型/穿搭以这些图为唯一标准，不要自己另起一个人设。"
    return s


def card_brief(c):
    """把结构化字段拼成「设定块」——给模型看的，也是界面上「预览」用的
    v1.1.1：用户要求人设按字段分开填，所以字段要真的进 prompt，
    否则填了「性格/口头禅/禁忌」等于没填。
    """
    c = c or {}
    lines = []
    name = (c.get("name") or "").strip()
    nick = (c.get("nickname") or "").strip()
    if name:
        lines.append(f"- 姓名：{name}" + (f"（别人也叫你「{nick}」）" if nick and nick != name else ""))
    if (c.get("gender") or "").strip():
        lines.append(f"- 性别：{c['gender'].strip()}")
    if (c.get("age") or "").strip():
        lines.append(f"- 年龄：{c['age'].strip()}")
    if (c.get("identity") or "").strip():
        lines.append(f"- 身份：{c['identity'].strip()}")
    if (c.get("personality") or "").strip():
        lines.append(f"- 性格：{c['personality'].strip()}")
    if (c.get("style") or "").strip():
        lines.append(f"- 说话风格：{c['style'].strip()}")
    catch = c.get("catchphrases") or []
    if catch:
        lines.append("- 口头禅（偶尔自然带出来，别每句都用）：" + "、".join(catch))
    taboo = c.get("taboos") or []
    if taboo:
        lines.append("- 雷区（绝对不要提）：" + "、".join(taboo))
    if (c.get("background") or "").strip():
        lines.append(f"- 背景：{c['background'].strip()}")
    if (c.get("greeting") or "").strip():
        lines.append(f"- 有人第一次私聊你，可以这样说：「{c['greeting'].strip()}」")
    if not lines:
        return ""
    out = "【角色设定】\n" + "\n".join(lines)
    ex = c.get("examples") or []
    if ex:
        out += "\n\n【说话示例】（学语气和长度，别照抄内容）"
        for it in ex[:8]:
            u = (it.get("user") or "").strip() or "……"
            b = (it.get("bot") or "").strip()
            if b:
                out += f"\n对方：{u}\n你：{b}"
    return out


def build_system_prompt(key=None):
    """拼 system prompt：设定块 + 正文 + 追加规则"""
    c = get_card(key)
    if not c:
        c = DEFAULT_CARD
    parts = []
    brief = card_brief(c)
    if brief:
        parts.append(brief)
    parts.append((c.get("persona") or "").strip())
    if (c.get("extra") or "").strip():
        parts.append(c["extra"].strip())
    return "\n\n".join(x for x in parts if x)


# ==================== 迁移 / 镜像 ====================

def migrate_from_legacy():
    """把老的 personas.json 拆成卡片（只在目录为空时做）"""
    ensure_dir()
    if load_cards():
        return 0
    path = legacy_json()
    if not os.path.exists(path):
        # 连老文件都没有：落一张默认卡
        save_card("default", DEFAULT_CARD)
        return 1
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        save_card("default", DEFAULT_CARD)
        return 1
    ps = (data.get("personas") or {}) if isinstance(data, dict) else {}
    if not isinstance(ps, dict) or not ps:
        save_card("default", DEFAULT_CARD)
        return 1
    active = str(data.get("active") or "")
    n = 0
    for k, v in ps.items():
        if not isinstance(v, dict):
            continue
        card = {
            "key": _safe_key(k),
            "name": v.get("name") or k,
            "desc": v.get("desc") or "",
            "emoji": v.get("emoji") or "🙂",
            "tags": ["迁移"],
            "persona": v.get("persona") or "",
            "extra": v.get("rules") or "",
        }
        if not card["persona"]:
            continue
        ok, _ = save_card(card["key"], card)
        if ok:
            n += 1
    if active and _safe_key(active) in load_cards():
        try:
            cfg_mod.update(persona_active=_safe_key(active))
        except Exception:
            pass
    if not load_cards():
        save_card("default", DEFAULT_CARD)
        n += 1
    _mirror_legacy()
    return n


def _mirror_legacy(active=None):
    """把卡片同步回 personas.json（老代码/老工具仍能读）"""
    try:
        cards = load_cards()
        if not cards:
            return
        act = active or active_key()
        data = {"active": act, "personas": {}}
        for k, c in cards.items():
            data["personas"][k] = {
                "name": c["name"], "persona": c["persona"],
                "rules": c.get("extra") or "",
                "desc": c.get("desc") or "", "emoji": c.get("emoji") or "",
            }
        path = legacy_json()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ==================== 导入导出 ====================

def export_card(key, path):
    c = get_card(key)
    if not c:
        return False, "卡片不存在"
    try:
        payload = {x: y for x, y in c.items() if not x.startswith("_")}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True, f"已导出到 {os.path.basename(path)}"
    except Exception as e:
        return False, f"导出失败：{e}"


def import_card(path, rename=""):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and "personas" in data:
            n = 0
            for k, v in (data["personas"] or {}).items():
                if isinstance(v, dict):
                    ok, _ = save_card(_safe_key(k), {**v, "key": _safe_key(k)})
                    n += 1 if ok else 0
            return (n > 0), f"从多人设文件导入了 {n} 张卡片"
        card = _norm_card(data, key=os.path.basename(path)[:-5])
        if rename:
            card["key"] = _safe_key(rename)
            card["name"] = rename
        ok, msg = save_card(card["key"], card)
        return ok, msg
    except Exception as e:
        return False, f"导入失败：{e}"


if __name__ == "__main__":
    import io
    import sys as _sys
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("卡片目录:", dir_path())
    print("迁移:", migrate_from_legacy())
    for c in list_cards():
        print(f"  [{'*' if c['active'] else ' '}] {c['emoji']} {c['name']} ({c['key']}) "
              f"正文{c['persona_len']}字 {c['updated_text']}")
    print("当前:", active_name())
    print("prompt 前 120 字:", build_system_prompt()[:120])
