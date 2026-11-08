# -*- coding: utf-8 -*-
"""多语言 —— 界面文字从 langs/*.json 读，外部可扩展

设计目标
--------
1. **外部可扩展**：往 langs/ 目录里丢一个 `<语言码>.json`，界面语言下拉里就会出现，
   不用改一行代码。语言码建议用 `zh-CN` / `en-US` / `ja-JP` 这种。
2. **缺 key 不炸**：任何语言缺的 key 都自动回落到 zh-CN 的内置文案，
   所以翻译一半也能用，不会出现空白按钮。
3. **Python 端也能用**：`t("key")` 在 agent.py / 日志里同样可用；
   界面上通过 `export()` 把整包字典交给前端，避免逐个 key 往返。

文件格式
--------
```json
{
  "name": "English",
  "native": "English",
  "author": "你的名字",
  "version": 1,
  "dict": {
    "tab.console": "Console",
    "btn.start": "Start"
  }
}
```
`name`/`native` 只用于下拉显示，`dict` 才是翻译。

内置的 zh-CN 在下面 `_BASE` 里，第一次运行会写出一份 langs/zh-CN.json 当模板。
"""
import json
import os
import re
import sys
import shutil
import threading

DEFAULT_LANG = "zh-CN"
LANG_DIR_NAME = "langs"

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
LANG_DIR = os.path.join(DATA_DIR, LANG_DIR_NAME)
# 程序自带的一份 langs/（装好后在 exe 旁边）。数据目录换了地方时，
# 这里的东西会被拷过去，避免「换了数据目录英文界面就空了」。
PROGRAM_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), LANG_DIR_NAME)

# v1.8.16：内置英文界面词表。安装包以前根本没打 langs/ 目录，PyInstaller 下
# PROGRAM_LANG_DIR 指向不存在的 _MEIPASS/langs，ensure_dir() 只能写一份没有 ui 段的
# 样例文件 —— 结果「切英语」后前端拿到的 UI_MAP 是空字典，一个字都不翻译。
# 现在把整份词表编译进代码，文件丢了也能翻；ensure_dir() 也用它来 seed。
try:
    from i18n_builtin_en import UI as _BUILTIN_UI, DICT as _BUILTIN_DICT
except Exception:
    _BUILTIN_UI, _BUILTIN_DICT = {}, {}
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8}){0,2}$")

# ---------------------------------------------------------------------------
# 内置中文文案 = 所有 key 的"权威清单"。
# 新增界面文字时，先在这里加一行，再在 index.html 里加 data-i18n="key"。
# ---------------------------------------------------------------------------
_BASE = {
    "app.title": "小鲸鱼 Agent 控制台",
    "app.subtitle": "源码版",
    "app.subtitle_pkg": "打包版",

    # 顶部 / 状态
    "status.running": "运行中",
    "status.stopped": "已停止",
    "status.model": "模型",
    "btn.start": "启动 Agent",
    "btn.stop": "停止 Agent",
    "btn.refresh": "刷新",
    "btn.save": "保存",
    "btn.save_restart": "保存并重启",
    "btn.cancel": "取消",
    "btn.close": "关闭",
    "btn.delete": "删除",
    "btn.open_dir": "打开目录",
    "btn.reload": "重新扫描",
    "btn.apply": "应用",
    "btn.known": "知道了",
    "btn.later": "稍后再说",

    # Tab
    "tab.console": "控制台",
    "tab.model": "模型设置",
    "tab.persona": "人设编辑",
    "tab.alive": "活人感",
    "tab.memory": "记忆查看",
    "tab.learn": "学习",
    "tab.admin": "管理员",
    "tab.affinity": "好感度",
    "tab.draw": "画图",
    "tab.settings": "设置",
    "btn.start_all": "一键启动",

    # 控制台
    "card.log": "运行日志",
    "log.autoscroll": "自动滚动",
    "log.clear": "清空",
    "log.lines": "行",
    "log.chars": "字符",

    # 模型设置
    "card.provider": "模型来源",
    "card.local_model": "本地 LM Studio 模型",
    "card.params": "参数",
    "card.usage": "用量看板",
    "usage.cloud_today": "今日云端",
    "usage.local_today": "今日本地",
    "usage.percent": "上限使用率",
    "usage.guard": "保护状态",
    "usage.days": "最近 7 天",
    "usage.kinds": "今日按用途",
    "usage.details": "最近明细",
    "usage.clear": "清空今日",

    # 人设
    "card.persona_edit": "人设（JSON）",
    "card.persona_list": "人设列表",

    # 活人感
    "card.stickers": "表情包管理",
    "card.moment": "自动说说",
    "card.care_plans": "主动关心计划",
    "card.comments": "说说评论回复",
    "care.source": "决策方式",
    "care.add": "手动加一条",
    "care.when": "时间",
    "care.reason": "理由",
    "care.status": "状态",
    "care.pending": "等待触发",
    "care.done": "已触发",
    "care.cancelled": "已取消",
    "comments.empty": "还没有收到评论",
    "comments.reply": "回复",

    # 记忆
    "mem.temp": "临时上下文",
    "mem.perm": "永久记忆",
    "mem.diary": "日记",
    "mem.bind_hint": "还没绑定账号 → 永久记忆不会被写入",
    "mem.export": "导出",
    "mem.import": "载入",
    "mem.rebuild": "重编译",
    "mem.open_db": "打开文件目录",

    # 设置
    "set.appearance": "外观",
    "set.background": "背景图",
    "set.system": "启动与系统",
    "set.napcat": "QQ 接入（NapCat）",
    "set.memory": "记忆策略",
    "set.notice": "公告与通知",
    "set.language": "语言",
    "set.uninstall": "卸载与数据保留",
    "set.update": "在线更新（GitHub）",
    "set.config_file": "配置文件",
    "set.theme": "主题",
    "set.icon": "自定义图标（左上角那个「鲸」）",
    "set.autostart": "开机自启动",
    "set.group_memory": "保存群聊记忆",
    "set.lang_hint": "语言包放在 langs/ 目录，丢一个 json 就能加一种语言",
    "set.bg_hint": "背景图放在 backgrounds/ 目录，选一张即可",
    "set.bg_dim": "压暗程度",
    "set.bg_blur": "模糊",

    # 通用提示
    "msg.saved": "已保存",
    "msg.save_failed": "保存失败",
    "msg.loading": "加载中…",
    "msg.empty": "（空）",
    "msg.confirm": "确认",
    "msg.danger": "危险操作",
    "msg.network_off": "连不上服务",
}


def ensure_dir(path=None):
    """确保 langs/ 存在，并写出内置中文模板与一份英文示例（只在缺失时写）。

    v8：如果程序目录自己带了一份 langs/（带完整英文界面翻译），
    先把缺的文件拷过来，再考虑写内置模板 —— 否则用户一旦把数据目录
    指到别处，英文界面就整片空白了。
    """
    d = path or LANG_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return d
    _copy_from_program_dir(d)
    zh = os.path.join(d, DEFAULT_LANG + ".json")
    if not os.path.exists(zh):
        _write_lang_file(zh, {
            "name": "简体中文",
            "native": "简体中文",
            "author": "内置",
            "version": 1,
            "dict": _BASE,
        })
    en = os.path.join(d, "en-US.json")
    if not os.path.exists(en):
        # v1.8.16：写的是**完整**英文表（含 ui 段），不再写那份 80 条的示例。
        # 没有这一步，界面语言切到 English 后整页仍是中文。
        _write_lang_file(en, {
            "name": "English",
            "native": "English",
            "author": "内置（可自行修改）",
            "version": "1.8.16",
            "dict": (_BUILTIN_DICT or _EN_SAMPLE),
            "ui": dict(_BUILTIN_UI or {}),
        })
    readme = os.path.join(d, "怎么加语言.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_LANG_README)
        except Exception:
            pass
    return d


def _copy_from_program_dir(d):
    """把程序自带 langs/ 里缺的文件补到数据目录（已有文件绝不覆盖）。"""
    try:
        src = PROGRAM_LANG_DIR
        if not src or os.path.abspath(src) == os.path.abspath(d) or not os.path.isdir(src):
            return
        for fn in os.listdir(src):
            if not (fn.lower().endswith(".json") or fn.lower().endswith(".txt")):
                continue
            dst = os.path.join(d, fn)
            if os.path.exists(dst):
                continue
            try:
                shutil.copyfile(os.path.join(src, fn), dst)
            except Exception:
                pass
    except Exception:
        pass


def _write_lang_file(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _read_lang_file(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _load_dir(path=None):
    """{code: {name, native, dict, path}}，坏文件跳过。

    v8：先读程序自带的那份 langs/，再读数据目录的 —— 同名语言数据目录优先
    （用户能自己改），但程序带的英文翻译不会因为数据目录换位置而丢。
    """
    d = path or LANG_DIR
    dirs = []
    for x in (PROGRAM_LANG_DIR, d):
        if x and os.path.isdir(x) and os.path.abspath(x) not in [os.path.abspath(y) for y in dirs]:
            dirs.append(x)
    out = {}
    for src in dirs:
        _load_one_dir(src, out)
    # v1.8.16：内置表兜底 —— 磁盘上那份缺 ui（老样例）或缺条目时补上
    if _BUILTIN_UI:
        item = out.get("en-US")
        if item is None:
            item = {"name": "English", "native": "English",
                    "dict": dict(_BUILTIN_DICT or {}), "ui": {}, "path": ""}
            out["en-US"] = item
        cur = dict(item.get("ui") or {})
        if len(cur) < len(_BUILTIN_UI):
            merged = dict(_BUILTIN_UI)
            merged.update(cur)          # 用户自己改过的覆盖内置
            item["ui"] = merged
    return out


def _load_one_dir(d, out):
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".json"):
            continue
        code = fn[:-5]
        if not _LANG_RE.match(code):
            continue
        data = _read_lang_file(os.path.join(d, fn))
        if not isinstance(data, dict):
            continue
        dic = data.get("dict")
        if not isinstance(dic, dict):
            # 允许"裸字典"：整个文件就是 key -> 文案
            dic = {k: v for k, v in data.items()
                   if isinstance(v, str) and "." in k}
        # ui 段：中文原文 -> 译文。界面全量翻译就用这一段，
        # 允许一个文件只有 ui 没有 dict（外部加语言时最省事）。
        ui = data.get("ui")
        if not isinstance(ui, dict):
            ui = {}
        ui = {str(k): str(v) for k, v in ui.items() if str(v).strip()}
        if not dic and not ui:
            continue
        prev = out.get(code) or {}
        # 同一个语言两边都有：以数据目录为准，但缺的条目用程序自带的补
        pui = dict(prev.get("ui") or {})
        pui.update(ui)
        pdic = dict(prev.get("dict") or {})
        pdic.update({str(k): str(v) for k, v in dic.items()})
        out[code] = {
            "name": str(data.get("name") or prev.get("name") or code),
            "native": str(data.get("native") or prev.get("native") or data.get("name") or code),
            "dict": pdic,
            "ui": pui,
            "path": os.path.join(d, fn),
        }


def ui_dict(code=None, path=None):
    """返回 {中文原文: 译文}。默认语言（中文）返回空字典。

    这个字典就是整套界面的翻译表：前端拿到后在 DOM 里做整句替换，
    所以不用给每个元素加 data-i18n，也不用改 HTML 结构。
    """
    code = code or current()
    if code == DEFAULT_LANG:
        return {}
    item = _load_dir(path).get(code)
    if not item:
        return {}
    return dict(item.get("ui") or {})


def available(path=None):
    """所有可用语言（内置 zh-CN 永远在，即使文件夹被删了）。"""
    ensure_dir(path)
    items = []
    items.append({
        "code": DEFAULT_LANG,
        "name": "简体中文",
        "native": "简体中文",
        "count": len(_BASE),
        "ui_count": 0,
        "builtin": True,
        "ok": True,
        "msg": "",
    })
    for code, info in _load_dir(path).items():
        if code == DEFAULT_LANG:
            # 用户可能改过 zh-CN.json，以文件为准，但 key 数量照实报
            items[0]["count"] = len(info["dict"])
            items[0]["ui_count"] = 0
            items[0]["path"] = info["path"]
            continue
        missing = [k for k in _BASE if k not in info["dict"]]
        items.append({
            "code": code,
            "name": info["name"],
            "native": info["native"],
            "count": len(info["dict"]),
            "ui_count": len(info.get("ui") or {}),
            "builtin": False,
            "ok": True,
            "missing": len(missing),
            "msg": ("缺 %d 条，会自动用中文补" % len(missing)) if missing else "完整",
        })
    return items


def _merged(code):
    """目标语言 + 中文兜底。"""
    table = dict(_BASE)
    if code and code != DEFAULT_LANG:
        info = _load_dir().get(code)
        if info:
            table.update(info["dict"])
    elif code == DEFAULT_LANG:
        info = _load_dir().get(DEFAULT_LANG)
        if info:
            table.update(info["dict"])
    return table


def current():
    try:
        import config as cfg_mod
        code = str(cfg_mod.get("lang") or DEFAULT_LANG)
    except Exception:
        code = DEFAULT_LANG
    codes = {x["code"] for x in available()}
    return code if code in codes else DEFAULT_LANG


def set_lang(code):
    """切换语言，返回 (ok, msg)。"""
    code = str(code or "").strip()
    codes = {x["code"] for x in available()}
    if code not in codes:
        return False, "没有这个语言：%s（可选：%s）" % (code, "、".join(sorted(codes)))
    try:
        import config as cfg_mod
        cfg_mod.set_checked("lang", code)
    except Exception as e:
        return False, "写配置失败：%s" % e
    return True, "界面语言已切换为 %s（刷新界面生效）" % code


def t(key, default=None, **kw):
    """取一条文案。缺 key 返回 default 或 key 本身，绝不抛异常。"""
    code = current()
    table = _merged(code)
    val = table.get(key)
    if val is None:
        val = _BASE.get(key)
    if val is None:
        val = default if default is not None else key
    if kw:
        try:
            return str(val).format(**kw)
        except Exception:
            return str(val)
    return str(val)


def export(code=None):
    """给前端的整包：{ok, code, name, items, dict}"""
    code = code or current()
    items = available()
    name = code
    for it in items:
        if it["code"] == code:
            name = it["name"]
            break
    return {
        "ok": True,
        "code": code,
        "name": name,
        "items": items,
        "dict": _merged(code),
        "base_count": len(_BASE),
    }


def missing_keys(code):
    """某个语言还缺哪些 key（翻译时对着这个补）。"""
    if code == DEFAULT_LANG:
        return []
    info = _load_dir().get(code)
    have = info["dict"] if info else {}
    return [k for k in _BASE if k not in have]


def keys():
    return list(_BASE.keys())


_EN_SAMPLE = {
    "app.title": "Whale Agent Console",
    "status.running": "Running",
    "status.stopped": "Stopped",
    "btn.start": "Start Agent",
    "btn.stop": "Stop Agent",
    "btn.refresh": "Refresh",
    "btn.save": "Save",
    "btn.save_restart": "Save & Restart",
    "btn.cancel": "Cancel",
    "btn.close": "Close",
    "btn.delete": "Delete",
    "btn.open_dir": "Open Folder",
    "btn.reload": "Rescan",
    "btn.apply": "Apply",
    "btn.known": "Got it",
    "btn.later": "Later",
    "tab.console": "Console",
    "tab.model": "Model",
    "tab.persona": "Persona",
    "tab.alive": "Aliveness",
    "tab.memory": "Memory",
    "tab.settings": "Settings",
    "card.log": "Runtime Log",
    "log.autoscroll": "Auto-scroll",
    "log.clear": "Clear",
    "card.provider": "Model Provider",
    "card.local_model": "Local LM Studio Model",
    "card.params": "Parameters",
    "card.usage": "Usage",
    "usage.cloud_today": "Cloud today",
    "usage.local_today": "Local today",
    "usage.percent": "Limit used",
    "usage.guard": "Guard",
    "usage.days": "Last 7 days",
    "usage.kinds": "By purpose today",
    "usage.details": "Recent details",
    "usage.clear": "Clear today",
    "card.stickers": "Stickers",
    "card.moment": "Auto Moments",
    "card.care_plans": "Care Plans",
    "card.comments": "Moment Comments",
    "care.source": "Decided by",
    "care.add": "Add one",
    "care.when": "When",
    "care.reason": "Reason",
    "care.status": "Status",
    "care.pending": "Pending",
    "care.done": "Fired",
    "care.cancelled": "Cancelled",
    "mem.temp": "Short-term",
    "mem.perm": "Long-term",
    "mem.diary": "Diary",
    "mem.export": "Export",
    "mem.import": "Import",
    "mem.rebuild": "Rebuild",
    "mem.open_db": "Open Folder",
    "set.appearance": "Appearance",
    "set.background": "Background",
    "set.system": "Startup & System",
    "set.napcat": "QQ Bridge (NapCat)",
    "set.memory": "Memory Policy",
    "set.notice": "Notices",
    "set.language": "Language",
    "set.update": "Online Update (GitHub)",
    "set.theme": "Theme",
    "set.bg_dim": "Dim",
    "set.bg_blur": "Blur",
    "msg.saved": "Saved",
    "msg.save_failed": "Save failed",
    "msg.empty": "(empty)",
}

_LANG_README = """怎么给界面加一种语言
======================

1. 复制本目录里的 zh-CN.json，改名成你要的语言码，例如：
     ja-JP.json   日语
     en-US.json   英语（已内置一份不完整的示例）
     zh-TW.json   繁体中文

   语言码格式：两三个字母，后面可以跟 -地区，比如 pt-BR、zh-TW。

2. 打开新文件，改这三个字段：
     "name"   : 下拉里显示的名字，例如 "日本語"
     "native" : 母语写法，也一样
     "author" : 你的名字（随便写）

3. 把 "dict" 里每一条的右边换成你的语言。左边（key）不要动。
   只翻你会的，没翻的会自动用中文显示，不会出现空白。

4. 保存，回到界面：设置 → 语言 → 刷新 → 选中 → 应用。

5. 想知道还缺哪些 key？界面会显示「缺 N 条」。
   或者命令行：
     python -c "import i18n; print(i18n.missing_keys('ja-JP'))"

注意
----
* 文案里如果有 {name} 这种大括号占位符，翻译时要把占位符原样保留。
* 这个目录不会被打包进程序包（属于用户数据），升级时不会被覆盖。
* 想临时看效果：直接改 lang 配置，或者设置里切一下就刷新。
"""

if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ensure_dir()
    print("lang dir:", LANG_DIR)
    for it in available():
        print("  ", it["code"], it["name"], it["count"], it.get("msg", ""))
    print("current:", current())
    print("样例:", t("tab.console"), "/", t("btn.start"))
