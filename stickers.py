# -*- coding: utf-8 -*-
"""表情包管理 —— 独立目录 + 标签索引 + emoji 装饰

设计目标（对应需求④）：
  1. 表情包放在**软件包旁边的一个独立目录**（默认 `stickers/`），
     用户自己往里丢图片就能用，不需要改代码、不需要重新打包。
  2. 每个表情包可以配"标签"（开心 / 生气 / 委屈 / 无语 …），
     机器人按当前语境挑一个贴切的发出去；没标签的就按文件名猜。
  3. 除了图片表情包，还支持在文字里夹 emoji（真·emoji 字符），
     这个不需要任何资源文件，纯文本就能发。

目录结构：
    <数据目录>/stickers/
        happy_01.gif
        doge.png
        无语.jpg
        _meta.json          ← 可选，{"doge.png": ["开心", "调皮"], ...}
        _disabled.json      ← 可选，["不想发的.png"]，列在这里的会被跳过

支持的格式：.png .jpg .jpeg .gif .webp .bmp
（QQ 对 gif/png 支持最好，webp 部分版本会转码失败，建议优先 png/gif）
"""
import json
import os
import random
import re

# ---------- 标签体系 ----------
# 每个情绪给一组同义词，用来把"模型想表达的情绪"映射到表情包标签
EMOTION_KEYWORDS = {
    "开心": ["开心", "高兴", "快乐", "兴奋", "happy", "joy", "笑", "哈哈", "好耶", "太棒"],
    "生气": ["生气", "愤怒", "气死", "火大", "angry", "怒", "烦", "恼"],
    "委屈": ["委屈", "难过", "伤心", "sad", "哭", "呜呜", "可怜", "emo"],
    "无语": ["无语", "无奈", "speechless", "汗", "尴尬", "沉默", "算了"],
    "害羞": ["害羞", "脸红", "不好意思", "shy", "羞涩", "腼腆"],
    "调皮": ["调皮", "坏笑", "搞怪", "playful", "嘿嘿", "逗", "鬼脸"],
    "惊讶": ["惊讶", "震惊", "吃惊", "surprise", "哇", "居然", "没想到"],
    "困": ["困", "累", "睡", "sleepy", "疲惫", "打哈欠", "躺"],
    "爱心": ["爱心", "喜欢", "love", "心动", "比心", "爱了", "爱你", "爱你哦", "抱抱"],
    "点赞": ["赞", "点赞", "厉害", "牛", "thumbsup", "棒", "优秀"],
    "吃饭": ["吃饭", "饿", "美食", "饭", "吃", "food", "干饭"],
    "打招呼": ["打招呼", "你好", "hello", "挥手", "在吗", "hi", "早安", "晚安", "早上好", "晚上好", "拜拜", "再见"],
}

# 纯 emoji（不需要任何图片资源），按情绪分组
EMOJI_SETS = {
    "开心": ["😄", "😆", "🥳", "😊", "🤣", "😁", "✨", "🎉"],
    "生气": ["😤", "😠", "🤬", "💢", "😾", "🔥"],
    "委屈": ["🥺", "😢", "😭", "😞", "🫠", "💧"],
    "无语": ["😑", "😅", "🙃", "😐", "🤐", "…"],
    "害羞": ["😳", "🙈", "😊", "☺️", "🫣"],
    "调皮": ["😜", "😝", "🤪", "😏", "😼", "👻"],
    "惊讶": ["😲", "😱", "🤯", "😮", "⁉️"],
    "困": ["😴", "🥱", "😪", "💤", "🛌"],
    "爱心": ["❤️", "💕", "💗", "😍", "🥰", "💞"],
    "点赞": ["👍", "👏", "🙌", "💪", "🫡", "⭐"],
    "吃饭": ["🍚", "🍜", "🍰", "🧋", "🍕", "🥢"],
    "打招呼": ["👋", "🙋", "🤗", "✋", "🫶"],
}

# 兜底通用 emoji（情绪判断不出来时用）
EMOJI_GENERIC = ["😊", "🙂", "😌", "✨", "🌊", "🐳", "👀", "🤔", "😶", "🫧"]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

_META_NAME = "_meta.json"
_DISABLED_NAME = "_disabled.json"


# ==================== 目录 ====================
def _data_dir():
    """和数据目录保持同一套规则（环境变量 > exe 同级 > 脚本目录）"""
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def sticker_dir(cfg_dir=None):
    """返回表情包目录的绝对路径（不保证存在）"""
    if cfg_dir:
        base = cfg_dir
        if not os.path.isabs(base):
            base = os.path.join(_data_dir(), base)
        return base
    return os.path.join(_data_dir(), "stickers")


def ensure_dir(path=None):
    """确保目录存在，并在首次创建时放一个说明文件，方便用户知道怎么用"""
    p = path or sticker_dir()
    os.makedirs(p, exist_ok=True)
    readme = os.path.join(p, "怎么用.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(
                    "把小鲸鱼能发的表情包图片直接丢进这个文件夹就行。\n"
                    "\n"
                    "支持的格式：.png  .jpg  .jpeg  .gif  .webp  .bmp\n"
                    "推荐 png / gif（QQ 兼容性最好）。\n"
                    "\n"
                    "【怎么让它按语境挑表情包】\n"
                    "文件名里带情绪词即可，例如：\n"
                    "    开心_好耶.png\n"
                    "    无语_汗.gif\n"
                    "    生气_炸毛.jpg\n"
                    "    委屈_呜呜.png\n"
                    "    点赞_厉害.gif\n"
                    "可用情绪词：" + "、".join(EMOTION_KEYWORDS.keys()) + "\n"
                    "\n"
                    "【更精确的做法：写 _meta.json】\n"
                    "在同一个目录里建 _meta.json，内容形如：\n"
                    '    {"doge.png": ["调皮", "开心"], "无语.gif": ["无语"]}\n'
                    "\n"
                    "【不想发某个表情包】\n"
                    "在 _disabled.json 里列出文件名，例如：\n"
                    '    ["不想发的.png"]\n'
                    "\n"
                    "注意：这个文件夹在 exe 同级目录（不是 _internal 里面），\n"
                    "换电脑时把整个 stickers 文件夹一起拷过去就行。\n"
                )
        except Exception:
            pass
    return p


# ==================== 索引 ====================
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _load_meta(dirpath):
    meta = _load_json(os.path.join(dirpath, _META_NAME), {})
    if not isinstance(meta, dict):
        meta = {}
    clean = {}
    for k, v in meta.items():
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            clean[str(k)] = [str(x) for x in v if str(x).strip()]
    return clean


def _load_disabled(dirpath):
    data = _load_json(os.path.join(dirpath, _DISABLED_NAME), [])
    if not isinstance(data, list):
        return set()
    return set(str(x) for x in data)


def _tags_from_filename(name):
    """从文件名里猜标签：先看完整文件名，再看用 _ - 空格 . 拆开的每一段"""
    stem = os.path.splitext(name)[0]
    hits = []
    low = stem.lower()
    for emotion, words in EMOTION_KEYWORDS.items():
        for w in words:
            if w.lower() in low:
                hits.append(emotion)
                break
    return hits


def list_stickers(cfg_dir=None, with_tags=True):
    """扫描表情包目录，返回 [{"name","path","size","tags"}]"""
    d = sticker_dir(cfg_dir)
    if not os.path.isdir(d):
        return []
    meta = _load_meta(d) if with_tags else {}
    disabled = _load_disabled(d) if with_tags else set()
    out = []
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return []
    for name in names:
        if name.startswith("_") or name.lower().endswith(".txt") or name.lower().endswith(".json"):
            continue
        if not name.lower().endswith(IMAGE_EXTS):
            continue
        if name in disabled:
            continue
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        tags = list(meta.get(name) or [])
        if not tags:
            tags = _tags_from_filename(name)
        try:
            size = os.path.getsize(full)
        except Exception:
            size = 0
        out.append({"name": name, "path": full, "size": size, "tags": tags})
    return out


def count_stickers(cfg_dir=None):
    return len(list_stickers(cfg_dir, with_tags=False))


# ==================== 挑一个 ====================
def detect_emotions(text):
    """从一段话里判断情绪标签（可能多个），按出现顺序去重"""
    if not text:
        return []
    low = str(text).lower()
    found = []
    for emotion, words in EMOTION_KEYWORDS.items():
        for w in words:
            if w.lower() in low:
                if emotion not in found:
                    found.append(emotion)
                break
    return found


def pick_sticker(text="", emotions=None, cfg_dir=None, rng=None):
    """按语境挑一个表情包，返回 {"name","path","tags"} 或 None"""
    pool = list_stickers(cfg_dir)
    if not pool:
        return None
    rng = rng or random
    tags = list(emotions or [])
    if not tags:
        tags = detect_emotions(text)

    if tags:
        # 命中标签的优先；同一个表情包命中多个标签时权重更高
        scored = []
        for s in pool:
            score = sum(1 for t in tags if t in (s.get("tags") or []))
            if score > 0:
                scored.append((score, s))
        if scored:
            top = max(x[0] for x in scored)
            cands = [s for sc, s in scored if sc == top]
            return rng.choice(cands)

    # 没命中 → 有一定概率随便发一个"无标签"的，营造随机感
    untagged = [s for s in pool if not s.get("tags")]
    if untagged and rng.random() < 0.5:
        return rng.choice(untagged)
    return None


def sticker_to_segment(path):
    """把本地图片路径转成 NapCat 的 image 消息段。

    实测两种都可用，这里用 base64://，因为它不依赖 NapCat 进程有没有权限读该路径
    （NapCat 与 agent 可能不在同一个用户/盘符下，file:// 会偶发失败）。
    为了不把超大 gif 塞进请求体，超过 4MB 时退回 file://。
    """
    import base64
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    if size and size > 4 * 1024 * 1024:
        uri = "file:///" + os.path.abspath(path).replace("\\", "/")
        return {"type": "image", "data": {"file": uri}}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        uri = "base64://" + base64.b64encode(raw).decode("ascii")
        return {"type": "image", "data": {"file": uri}}
    except Exception:
        uri = "file:///" + os.path.abspath(path).replace("\\", "/")
        return {"type": "image", "data": {"file": uri}}


# ==================== emoji 装饰 ====================
def pick_emoji(text="", emotions=None, rng=None):
    """按语境挑一个 emoji 字符，返回 "" 表示不加"""
    rng = rng or random
    tags = list(emotions or [])
    if not tags:
        tags = detect_emotions(text)
    if tags:
        t = rng.choice(tags)
        pool = EMOJI_SETS.get(t)
        if pool:
            return rng.choice(pool)
    return rng.choice(EMOJI_GENERIC)


def decorate_emoji(text, chance=0.35, rng=None):
    """有一定概率在回复末尾加个 emoji。

    只在"看起来适合加"的时候加：太短的、已经带 emoji 的、以问号结尾的都不动。
    """
    rng = rng or random
    if not text or len(text) < 4:
        return text
    if rng.random() >= chance:
        return text
    # 已经有 emoji 就不重复加
    if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text):
        return text
    # 以问号/省略号结尾的先不加，避免"在吗😄"这种怪味
    if text.rstrip().endswith(("?", "？", "…", "...")):
        return text
    e = pick_emoji(text, rng=rng)
    return (text + e) if e else text


def stats(cfg_dir=None):
    """给 GUI 用：目录、总数、按标签统计"""
    d = sticker_dir(cfg_dir)
    items = list_stickers(cfg_dir)
    by_tag = {}
    for s in items:
        for t in (s.get("tags") or ["未标注"]):
            by_tag[t] = by_tag.get(t, 0) + 1
    return {
        "dir": d,
        "exists": os.path.isdir(d),
        "count": len(items),
        "by_tag": by_tag,
        "items": [
            {"name": s["name"], "size": s["size"], "tags": s["tags"]}
            for s in items[:200]
        ],
    }


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ensure_dir()
    st = stats()
    print("表情包目录:", st["dir"])
    print("数量:", st["count"])
    print("按标签:", st["by_tag"])
    for it in st["items"][:20]:
        print("  -", it["name"], it["size"], it["tags"])
