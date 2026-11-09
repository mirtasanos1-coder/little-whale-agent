# -*- coding: utf-8 -*-
"""主题管理 —— 界面配色可通过文件扩展

设计目标：
  1. 内置几套主题（dark / light / ocean / sakura / forest），开箱即用；
  2. 用户可以在 themes/ 目录丢一个 <名字>.json，重启界面就能选到，
     不需要改任何 Python 代码 —— 这就是"可通过文件扩展"的含义；
  3. 主题文件坏了不能把界面搞挂：校验不过就退回内置 dark，并给出原因。

主题 JSON 格式（所有字段都可省略，省略的用 dark 的对应值补齐）：
  {
    "name": "我的主题",
    "vars": {
      "bg":       "#0b1220",   // 页面底色
      "panel":    "#111a2e",   // 卡片/面板底色
      "panel2":   "#16223c",   // 次级面板（输入框、表格斑马纹）
      "line":     "#22304d",   // 边框线
      "text":     "#e6edf7",   // 主文字
      "muted":    "#8fa3c0",   // 次要文字
      "accent":   "#38bdf8",   // 主色（按钮、选中态、进度条）
      "accent2":  "#22d3ee",   // 渐变色第二段
      "ok":       "#34d399",   // 成功
      "warn":     "#fbbf24",   // 警告
      "err":      "#f87171",   // 错误
      "radius":   "14px"       // 圆角（字符串，带单位）
    },
    "dark": true                 // 是否深色（影响滚动条/原生控件配色）
  }

用法：
    import theme
    theme.list_themes()            -> [{key,name,dark,builtin,path,ok,msg}]
    theme.get_theme("ocean")       -> {"key","name","dark","vars":{...}}
    theme.css_vars("ocean")        -> "--bg:#...;--panel:#...;"  直接塞进 <style>
    theme.ensure_dir()             -> 创建 themes/ 并写一份示例 + 说明
"""
import json
import os
import re
import sys
import threading

_lock = threading.RLock()


def _data_dir():
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()

try:
    import config as _cfg_mod
    _THEME_DIR_NAME = _cfg_mod.get("theme_dir") or "themes"
except Exception:
    _THEME_DIR_NAME = "themes"

THEME_DIR = os.path.join(DATA_DIR, _THEME_DIR_NAME)


# ==================== 内置主题 ====================
# 键名必须与 index.html 里 var(--bg) 之类一一对应，加新变量时两边都要加。
_DARK = {
    "bg": "#0b1220",
    "panel": "#111a2e",
    "panel2": "#16223c",
    "line": "#22304d",
    "text": "#e6edf7",
    "muted": "#8fa3c0",
    "accent": "#38bdf8",
    "accent2": "#22d3ee",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "err": "#f87171",
    "radius": "14px",
}

BUILTIN = {
    "dark": {
        "name": "深海蓝（默认）",
        "dark": True,
        "vars": dict(_DARK),
    },
    "light": {
        "name": "日光白",
        "dark": False,
        "vars": {
            "bg": "#f5f7fb",
            "panel": "#ffffff",
            "panel2": "#eef2f9",
            "line": "#dbe3ef",
            "text": "#1a2436",
            "muted": "#5b6b85",
            "accent": "#2563eb",
            "accent2": "#0ea5e9",
            "ok": "#059669",
            "warn": "#d97706",
            "err": "#dc2626",
            "radius": "14px",
        },
    },
    "ocean": {
        "name": "浅海青",
        "dark": True,
        "vars": {
            "bg": "#06181c",
            "panel": "#0a2530",
            "panel2": "#0e3140",
            "line": "#154a5e",
            "text": "#dff6fb",
            "muted": "#7fb6c6",
            "accent": "#2dd4bf",
            "accent2": "#5eead4",
            "ok": "#4ade80",
            "warn": "#facc15",
            "err": "#fb7185",
            "radius": "14px",
        },
    },
    "sakura": {
        "name": "樱粉",
        "dark": False,
        "vars": {
            "bg": "#fff7fa",
            "panel": "#ffffff",
            "panel2": "#ffeef4",
            "line": "#f7d3e0",
            "text": "#3b1f2b",
            "muted": "#8a6272",
            "accent": "#ec4899",
            "accent2": "#f472b6",
            "ok": "#10b981",
            "warn": "#f59e0b",
            "err": "#ef4444",
            "radius": "16px",
        },
    },
    "forest": {
        "name": "苔绿",
        "dark": True,
        "vars": {
            "bg": "#0d1512",
            "panel": "#132019",
            "panel2": "#1a2b21",
            "line": "#24402f",
            "text": "#e3f0e7",
            "muted": "#8aab97",
            "accent": "#4ade80",
            "accent2": "#86efac",
            "ok": "#34d399",
            "warn": "#fbbf24",
            "err": "#f87171",
            "radius": "14px",
        },
    },
    # v1.1.2 #26：雪夜（带下雪 + 卡片上沿积雪）
    "snow": {
        "name": "雪夜",
        "dark": True,
        "effects": {"snow": 46, "edge": True},
        "vars": {
            "bg": "#0b1220",
            "panel": "#121e33",
            "panel2": "#182742",
            "line": "#24395c",
            "text": "#eaf2ff",
            "muted": "#93a9c9",
            "accent": "#7dd3fc",
            "accent2": "#bae6fd",
            "ok": "#34d399",
            "warn": "#fbbf24",
            "err": "#f87171",
            "radius": "14px",
        },
    },
    # v1.1.3 任务七：金秋（低饱和金黄橙红 + 枫叶/银杏飘落）
    "autumn": {
        "name": "金秋",
        "dark": True,
        "effects": {"leaves": True, "edge": False},
        "vars": {
            "bg": "#1a1208",
            "panel": "#241a0d",
            "panel2": "#2f2211",
            "line": "#4a3517",
            "text": "#fdf6e3",
            "muted": "#c9b083",
            "accent": "#e8a33d",
            "accent2": "#f5c877",
            "ok": "#a3b86c",
            "warn": "#e8a33d",
            "err": "#d2691e",
            "radius": "14px",
        },
    },
}

DEFAULT_THEME = "dark"

# v1.1.3：主题可以带一点点"特效"：
#   snow   下雪（数量 8~120）
#   leaves 飘叶（枫叶/银杏，金秋用）
#   edge   卡片上沿一条积雪/高光
# 只认这几个键，别的键直接忽略（主题文件可能是别人给的）。
_EFFECT_KEYS = ("snow", "leaves", "edge")


def _norm_effects(raw):
    """把主题里的 effects 字段规范化成 {snow:int, leaves:bool, edge:bool}"""
    out = {"snow": 0, "leaves": False, "edge": False}
    if not isinstance(raw, dict):
        return out
    if raw.get("snow"):
        try:
            n = int(raw.get("snow"))
        except (TypeError, ValueError):
            n = 40
        out["snow"] = max(8, min(120, n))
    out["leaves"] = bool(raw.get("leaves"))
    out["edge"] = bool(raw.get("edge"))
    return out

# 只允许这些 CSS 变量被主题文件覆盖（防止用户写出奇怪的东西）
_ALLOWED_VARS = tuple(_DARK.keys())

# 颜色值只接受 #rgb / #rrggbb / #rrggbbaa / rgb()/rgba()/hsl()/渐变关键字，
# radius 额外接受数字+单位。这样即使主题文件是别人给的，也塞不进 CSS 注入。
_COLOR_RE = re.compile(
    r"^("
    r"#[0-9a-fA-F]{3,8}"
    r"|rgba?\(\s*[\d.%\s,]+\)"
    r"|hsla?\(\s*[\d.%\s,deg]+\)"
    r"|transparent|currentColor"
    r")$"
)
_RADIUS_RE = re.compile(r"^\d{1,3}(px|rem|em|%)$")


def _safe_value(key, val):
    """校验单个变量值，不合法返回 None"""
    if not isinstance(val, str):
        return None
    val = val.strip()
    if not val or len(val) > 60:
        return None
    if key == "radius":
        return val if _RADIUS_RE.match(val) else None
    return val if _COLOR_RE.match(val) else None


def validate(data):
    """校验一份主题数据，返回 (ok, msg, normalized)"""
    if not isinstance(data, dict):
        return False, "主题必须是一个 JSON 对象", None

    raw_vars = data.get("vars")
    if raw_vars is None:
        raw_vars = {k: v for k, v in data.items()
                    if k in _ALLOWED_VARS}       # 也允许把颜色写在顶层
    if not isinstance(raw_vars, dict):
        return False, "vars 必须是对象", None
    if not raw_vars:
        return False, "主题里没有任何颜色变量", None

    merged = dict(_DARK)
    bad = []
    for k, v in raw_vars.items():
        if k not in _ALLOWED_VARS:
            bad.append(f"{k}(未知变量)")
            continue
        sv = _safe_value(k, v)
        if sv is None:
            bad.append(f"{k}={v!r}(不是合法颜色)")
            continue
        merged[k] = sv

    if bad:
        return False, "以下字段不合法：" + "、".join(bad[:6]), None

    name = str(data.get("name") or "").strip()[:40] or "自定义主题"
    dark = data.get("dark")
    if dark is None:
        # 没写就按底色亮度猜：bg 越暗越可能是深色
        bg = merged.get("bg", "#000000")
        dark = _is_dark_hex(bg)
    return True, "ok", {"name": name, "dark": bool(dark), "vars": merged,
                        "effects": _norm_effects(data.get("effects"))}


def _is_dark_hex(color):
    """粗略判断颜色深浅（只处理 #rrggbb / #rgb）"""
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) < 6:
        return True
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return True
    # 感知亮度
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


# ==================== 目录与文件 ====================
def ensure_dir(path=None):
    """创建主题目录，并放一份示例主题 + 说明（已存在则不动）"""
    d = path or THEME_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return d

    sample = os.path.join(d, "示例-午夜紫.json")
    if not os.path.exists(sample):
        try:
            with open(sample, "w", encoding="utf-8") as f:
                json.dump({
                    "name": "午夜紫",
                    "dark": True,
                    "vars": {
                        "bg": "#0f0a1a",
                        "panel": "#171029",
                        "panel2": "#1f1638",
                        "line": "#312350",
                        "text": "#ece7f8",
                        "muted": "#a394c9",
                        "accent": "#a78bfa",
                        "accent2": "#c4b5fd",
                        "ok": "#34d399",
                        "warn": "#fbbf24",
                        "err": "#f87171",
                        "radius": "14px",
                    },
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    readme = os.path.join(d, "怎么自定义主题.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_README_TEXT)
        except Exception:
            pass
    return d


_README_TEXT = """\
========================================
 小鲸鱼 · 自定义主题说明
========================================

一、怎么加一个新主题
  1. 在本目录（themes/）新建一个文件，名字随你，比如  我的主题.json
     —— 文件名就是主题的 key，中文也可以。
  2. 内容照抄「示例-午夜紫.json」，改颜色就行。
  3. 回到界面「设置 → 外观 → 主题」，点「刷新主题」，新主题就出现在下拉框里。
  4. 选中它，点「应用主题」，界面立刻换色。

二、可改的颜色变量（只能写这几个，写错会被拒绝并告诉你哪个字段错）
  bg       页面底色
  panel    卡片 / 面板底色
  panel2   次级面板（输入框、表格斑马纹）
  line     边框线
  text     主文字颜色
  muted    次要文字颜色
  accent   主色（按钮、选中态、进度条）
  accent2  渐变色第二段
  ok       成功提示（绿）
  warn     警告提示（黄）
  err      错误提示（红）
  radius   圆角，形如 14px / 1rem

三、颜色写法
  支持 #rgb、#rrggbb、#rrggbbaa、rgb(...)、rgba(...)、hsl(...)、transparent。
  其他写法（比如直接写 red 或 url(...)）会被拒绝 —— 这是为了防止
  别人给你的主题文件里塞进奇怪的东西。

四、dark 字段
  "dark": true  深色主题（滚动条等原生控件会用深色）
  "dark": false 浅色主题
  不写的话程序会按 bg 的亮度自动猜。

五、改坏了怎么办
  · 某个字段写错 → 界面上该主题显示为"不可用"，并给出具体哪个字段错，
    程序会自动退回内置的 dark，界面不会白屏。
  · 想彻底恢复 → 删掉这个 json 文件即可。
  · 内置主题（dark / light / ocean / sakura / forest）删不掉，
    它们写在 theme.py 的 BUILTIN 里。
"""


def _load_file(path):
    """读并校验一个主题文件，返回 (key, theme_dict 或 None, msg)"""
    key = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return key, None, f"JSON 语法错误（第 {e.lineno} 行）：{e.msg}"
    except Exception as e:
        return key, None, f"读取失败：{e}"
    ok, msg, norm = validate(data)
    if not ok:
        return key, None, msg
    return key, norm, "ok"


def list_themes(theme_dir=None):
    """列出全部主题：内置 + themes/*.json

    返回 [{"key","name","dark","builtin","path","ok","msg"}, ...]
    不可用的自定义主题也会列出来（ok=False + msg），方便用户知道哪写错了。
    """
    d = theme_dir or THEME_DIR
    out = []
    for key, t in BUILTIN.items():
        out.append({"key": key, "name": t["name"], "dark": t["dark"],
                    "builtin": True, "path": "", "ok": True, "msg": "内置"})
    try:
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if not name.lower().endswith(".json"):
                    continue
                path = os.path.join(d, name)
                if not os.path.isfile(path):
                    continue
                key, norm, msg = _load_file(path)
                if key in BUILTIN:
                    # 自定义文件不允许覆盖内置主题名，避免"删了文件主题消失"的困惑
                    out.append({"key": key, "name": key, "dark": True, "builtin": False,
                                "path": path, "ok": False,
                                "msg": f"与内置主题重名，已忽略（请改个文件名）"})
                    continue
                if norm is None:
                    out.append({"key": key, "name": key, "dark": True, "builtin": False,
                                "path": path, "ok": False, "msg": msg})
                else:
                    out.append({"key": key, "name": norm["name"], "dark": norm["dark"],
                                "builtin": False, "path": path, "ok": True, "msg": "ok"})
    except Exception:
        pass
    return out


def get_theme(key=None, theme_dir=None):
    """取一个主题的最终数据。找不到 / 不可用 → 退回内置 dark。"""
    if not key:
        try:
            import config as _c
            key = _c.get("theme") or DEFAULT_THEME
        except Exception:
            key = DEFAULT_THEME

    if key in BUILTIN:
        t = BUILTIN[key]
        return {"key": key, "name": t["name"], "dark": t["dark"],
                "vars": dict(t["vars"]), "fallback": False,
                "effects": _norm_effects(t.get("effects"))}

    d = theme_dir or THEME_DIR
    path = os.path.join(d, str(key) + ".json")
    if os.path.isfile(path):
        k, norm, msg = _load_file(path)
        if norm:
            return {"key": k, "name": norm["name"], "dark": norm["dark"],
                    "vars": norm["vars"], "fallback": False,
                    "effects": _norm_effects(norm.get("effects"))}

    t = BUILTIN[DEFAULT_THEME]
    return {"key": DEFAULT_THEME, "name": t["name"], "dark": t["dark"],
            "vars": dict(t["vars"]), "fallback": True,
            "effects": _norm_effects(t.get("effects")),
            "msg": f"主题 {key!r} 不可用，已退回 {DEFAULT_THEME}"}


def css_vars(key=None, theme_dir=None):
    """生成可直接写进 <style> 的 CSS 变量串"""
    t = get_theme(key, theme_dir)
    parts = [f"--{k}:{v}" for k, v in t["vars"].items()]
    return ";".join(parts) + ";"


def current():
    """当前生效的主题（读 config.json）"""
    try:
        import config as _c
        key = _c.get("theme") or DEFAULT_THEME
    except Exception:
        key = DEFAULT_THEME
    t = get_theme(key)
    t["configured"] = key
    return t


def set_theme(key):
    """切换主题并落盘。返回 (ok, msg)"""
    key = str(key or "").strip()
    if not key:
        return False, "主题名不能为空"
    all_themes = {t["key"]: t for t in list_themes()}
    if key not in all_themes:
        return False, f"没有这个主题：{key}"
    if not all_themes[key]["ok"]:
        return False, f"主题 {key} 不可用：{all_themes[key]['msg']}"
    try:
        import config as _c
        _c.set_value("theme", key)
    except Exception as e:
        return False, f"写入配置失败：{e}"
    return True, f"已切换到主题「{all_themes[key]['name']}」"


def delete_theme(key):
    """删除一个自定义主题文件（内置主题不允许删）"""
    key = str(key or "").strip()
    if key in BUILTIN:
        return False, "内置主题不能删除"
    path = os.path.join(THEME_DIR, key + ".json")
    if not os.path.isfile(path):
        return False, "主题文件不存在"
    try:
        os.remove(path)
    except Exception as e:
        return False, f"删除失败：{e}"
    # 如果删的正好是当前主题，退回 dark
    try:
        import config as _c
        if (_c.get("theme") or "") == key:
            _c.set_value("theme", DEFAULT_THEME)
    except Exception:
        pass
    return True, f"已删除主题 {key}"


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("主题目录:", THEME_DIR)
    ensure_dir()
    for t in list_themes():
        flag = "OK " if t["ok"] else "BAD"
        print(f"  [{flag}] {t['key']:<16} {t['name']:<14} dark={t['dark']} "
              f"{'内置' if t['builtin'] else t['msg']}")
    print("\n当前主题:", current()["key"], current()["name"])
    print("CSS:", css_vars()[:120], "…")
